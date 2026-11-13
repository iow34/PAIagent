"""Register three domains, deliver data/file, run a profile and enable a trigger.

Only HTTP and local public fixtures; each invocation creates a fresh demo source.
"""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from client import Client  # pyright: ignore[reportImplicitRelativeImport]

HERE = Path(__file__).resolve().parent


def run(
    client: Client,
    *,
    capability: str = "search",
    mode: str = "async",
    domain: str = "tasks",
) -> dict[str, Any]:
    source = client.request(
        "POST", "/v1/sources", json={"name": "demo-" + uuid4().hex[:16]}
    )
    collections = {}
    deliveries = []
    envelopes = {}
    for name in ("tasks", "books", "learning_sessions"):
        example = json.loads((HERE / "domains" / f"{name}.json").read_text())
        collection = client.request(
            "POST",
            f"/v1/sources/{source['id']}/collections",
            json=example["collection"],
        )
        collections[name] = collection["id"]
        route = f"/v1/collections/{collection['id']}"
        client.request("POST", route + "/schemas", json=example["schema"])
        envelope = {
            "api_version": "1",
            "source_id": source["id"],
            "collection_id": collection["id"],
            "schema_version": 1,
            "operation": example["operation"],
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": example["payload"],
        }
        event = example["operation"] == "observe"
        envelope.update(
            {"event_id": "session-1"}
            if event
            else {"external_id": "item-1", "object_version": 1}
        )
        envelopes[name] = envelope
        client.request("POST", route + "/validate", json=envelope)
        endpoint = route + ("/events/single" if event else "/records/single")
        ack = client.request("POST", endpoint, replay_safe=True, json=envelope)
        replay = client.request("POST", endpoint, replay_safe=True, json=envelope)
        deliveries.append({"collection": name, "ack": ack, "replay": replay})
        if name == "books":
            file_envelope = {**envelope, "external_id": "book-file"}
            client.request(
                "POST",
                route + "/files",
                replay_safe=True,
                files={
                    "envelope": (None, json.dumps(file_envelope)),
                    "file": (
                        "book.md",
                        b"# Python\nA short introduction to Python programming.",
                        "text/markdown",
                    ),
                },
            )
    template = json.loads((HERE / "profiles" / f"{capability}.json").read_text())
    binding = json.loads((HERE / "domain-bindings.json").read_text())[domain]
    selection = template["selections"][0]
    selection.update(
        collection_id=collections[domain],
        fields=binding["fields"],
        kind=binding["kind"],
        mode=binding["mode"],
    )
    for metric in template["metrics"]:
        if metric["field"] is not None:
            metric["field"] = binding["quantity"]
        if metric["group_by"] is not None:
            metric["group_by"] = binding["category"]
    profile = client.request("POST", "/v1/profiles", json=template)
    body = {
        "profile_id": profile["profile_id"],
        "input": {"version": 1, "query": binding["query"]},
        "idempotency_key": uuid4().hex,
        "mode": mode,
    }
    accepted = client.request("POST", "/v1/runs", replay_safe=True, json=body)
    replay = client.request("POST", "/v1/runs", replay_safe=True, json=body)
    if accepted["id"] != replay["id"]:
        raise RuntimeError("Idempotent run identity changed")
    result = client.result(accepted)
    trigger = client.request(
        "PUT",
        f"/v1/profiles/{profile['profile_id']}/triggers",
        json={
            "version": 1,
            "enabled": True,
            "on_change": True,
            "interval_seconds": 3600,
            "debounce_seconds": 1,
            "max_wait_seconds": 10,
            "query": binding["query"],
        },
    )
    # Make a post-configuration change so on_change has a real input mutation.
    changed = {**envelopes[domain], "occurred_at": datetime.now(UTC).isoformat()}
    event = binding["kind"] == "event"
    changed.update({"event_id": "session-2"} if event else {"object_version": 2})
    client.request(
        "POST",
        f"/v1/collections/{collections[domain]}/"
        + ("events/single" if event else "records/single"),
        replay_safe=True,
        json=changed,
    )
    return {
        "source_id": source["id"],
        "collections": collections,
        "deliveries": deliveries,
        "profile_id": profile["profile_id"],
        "run_id": accepted["id"],
        "result": result,
        "trigger": trigger,
        "profiles": list(client.pages("/v1/profiles", limit=1)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--capability",
        choices=("search", "query", "summarize", "extract", "analyze", "recommend"),
        default="search",
    )
    parser.add_argument("--mode", choices=("async", "sync"), default="async")
    parser.add_argument(
        "--domain", choices=("tasks", "books", "learning_sessions"), default="tasks"
    )
    args = parser.parse_args()
    client = Client(
        os.environ.get("PAIAGENT_URL", "http://127.0.0.1:8000"),
        os.environ["PAIAGENT_SERVICE_KEY"],
        os.environ["PAIAGENT_USER_ID"],
    )
    try:
        print(
            json.dumps(
                run(
                    client,
                    capability=args.capability,
                    mode=args.mode,
                    domain=args.domain,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
