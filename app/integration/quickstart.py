"""Independent HTTP client: python -m app.integration.quickstart.

Set PAIAGENT_URL, PAIAGENT_API_KEY and PAIAGENT_USER_ID. Creates fresh demo
registry entries and validates all three payloads. Never calls a model.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx


def run(client: httpx.Client) -> list[dict[str, Any]]:
    def post(path: str, data: dict[str, Any]) -> dict[str, Any]:
        response = client.post(path, json=data)
        response.raise_for_status()
        return response.json()

    source = post("/v1/sources", {"name": "demo-" + uuid4().hex[:12]})
    results = []
    for path in sorted(Path(__file__).with_name("examples").glob("*.json")):
        example = json.loads(path.read_text())
        collection = post(
            f"/v1/sources/{source['id']}/collections", example["collection"]
        )
        route = f"/v1/collections/{collection['id']}"
        post(route + "/schemas", example["schema"])
        envelope = {
            "api_version": "1",
            "source_id": source["id"],
            "collection_id": collection["id"],
            "schema_version": 1,
            "operation": example["operation"],
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": example["payload"],
        }
        if example["operation"] == "observe":
            envelope["event_id"] = str(uuid4())
        else:
            envelope.update(external_id="demo-1", object_version=1)
        result = post(route + "/validate", envelope)
        results.append(
            {"collection": collection["name"], "id": collection["id"], **result}
        )
    return results


def main() -> None:
    with httpx.Client(
        base_url=os.environ.get("PAIAGENT_URL", "http://127.0.0.1:8000"),
        headers={
            "Authorization": "Bearer " + os.environ["PAIAGENT_API_KEY"],
            "X-User-ID": os.environ["PAIAGENT_USER_ID"],
        },
        timeout=30,
    ) as client:
        print(json.dumps(run(client), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
