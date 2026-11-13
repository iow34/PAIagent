"""Explicit preference, feedback and forgetting for a fresh demo user.

Use the collection/result IDs returned by quickstart. Does not enable profile
personalization: both owner and profile consent are required for actual use.
"""

import json
import os
from typing import Any
from uuid import uuid4

from client import Client  # pyright: ignore[reportImplicitRelativeImport]


def run(client: Client, collection_id: str, result_id: str) -> dict[str, Any]:
    settings = client.request("GET", "/v1/memory/settings", replay_safe=True)
    if settings["version"] != 0:
        raise ValueError(
            "Use a fresh demo user; this example does not replace existing memory settings."
        )
    config = {
        "expected_version": 0,
        "enabled": True,
        "collections": [collection_id],
        "definitions": [
            {
                "key": "response_format",
                "value_schema": {
                    "type": "string",
                    "maxLength": 20,
                    "enum": ["brief", "detailed"],
                },
            }
        ],
    }
    enabled = client.request("PUT", "/v1/memory/settings", json=config)
    preference = client.request(
        "PUT",
        "/v1/memory/preferences",
        json={
            "collection_id": collection_id,
            "key": "response_format",
            "value": "brief",
            "expected_version": 0,
        },
    )
    feedback_body = {"request_id": "demo-" + uuid4().hex, "rating": "helpful"}
    feedback = client.request(
        "POST",
        f"/v1/results/{result_id}/feedback",
        replay_safe=True,
        json=feedback_body,
    )
    replay = client.request(
        "POST",
        f"/v1/results/{result_id}/feedback",
        replay_safe=True,
        json=feedback_body,
    )
    if feedback["id"] != replay["id"]:
        raise RuntimeError("Feedback replay identity changed")
    forgotten = client.request("DELETE", f"/v1/memory/items/{preference['id']}")
    current = client.request("GET", "/v1/memory/settings", replay_safe=True)
    disabled = client.request(
        "PUT",
        "/v1/memory/settings",
        json={**config, "expected_version": current["version"], "enabled": False},
    )
    return {
        "settings": enabled,
        "preference": preference,
        "feedback": feedback,
        "forgotten": forgotten,
        "disabled": disabled,
    }


def main() -> None:
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
                    os.environ["PAIAGENT_COLLECTION_ID"],
                    os.environ["PAIAGENT_RESULT_ID"],
                ),
                indent=2,
            )
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
