"""Source-side transactional outbox example; no PAIagent internals are imported.

The source commits its object and outgoing envelope together, then delivers the
exact stored bytes after commit. A crash after acceptance but before local ack
causes a safe retry. This small example has one synchronous sender per SQLite DB;
production sources should use their existing DB and claim/lease pending rows.
"""

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Literal

import httpx


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS source_objects (
            collection_id TEXT NOT NULL, external_id TEXT NOT NULL,
            version INTEGER NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(collection_id, external_id)
        );
        CREATE TABLE IF NOT EXISTS source_outbox (
            event_id TEXT PRIMARY KEY, collection_id TEXT NOT NULL,
            body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            error_status INTEGER
        );
    """)


def commit_change(connection: sqlite3.Connection, envelope: dict[str, Any]) -> None:
    """Call in the source backend; duplicate IDs/versions abort the local commit."""
    if envelope["operation"] != "upsert":
        raise ValueError("This example commits full-state upserts.")
    body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    with connection:
        existing = connection.execute(
            "SELECT version FROM source_objects WHERE collection_id=? AND external_id=?",
            (envelope["collection_id"], envelope["external_id"]),
        ).fetchone()
        if existing and existing[0] >= envelope["object_version"]:
            raise ValueError("The source object version must increase.")
        connection.execute(
            "INSERT INTO source_objects VALUES (?,?,?,?) ON CONFLICT(collection_id,external_id) "
            "DO UPDATE SET version=excluded.version,payload=excluded.payload",
            (
                envelope["collection_id"],
                envelope["external_id"],
                envelope["object_version"],
                json.dumps(envelope["payload"], ensure_ascii=False),
            ),
        )
        connection.execute(
            "INSERT INTO source_outbox(event_id,collection_id,body) VALUES (?,?,?)",
            (envelope["event_id"], envelope["collection_id"], body),
        )


def deliver_one(
    connection: sqlite3.Connection, client: httpx.Client
) -> Literal["empty", "delivered", "retry", "rejected"]:
    if connection.in_transaction:
        raise RuntimeError("Commit the source transaction before making network calls.")
    row = connection.execute(
        "SELECT event_id,collection_id,body FROM source_outbox WHERE state='pending' ORDER BY rowid LIMIT 1"
    ).fetchone()
    if row is None:
        return "empty"
    event_id, collection_id, body = row
    try:
        response = client.post(
            f"/v1/collections/{collection_id}/records/single",
            content=body.encode(),
            headers={"Content-Type": "application/json"},
        )
    except httpx.TransportError:
        return "retry"  # unknown outcome: the row and exact event bytes survive
    if response.status_code == 429 or response.status_code >= 500:
        return "retry"  # caller schedules backoff; respect RateLimit-Reset on 429
    if response.status_code == 200:
        try:
            ack = response.json()
        except ValueError:
            return "retry"
        if (
            not isinstance(ack, dict)
            or ack.get("status") not in {"accepted", "duplicate", "stale"}
            or not ack.get("delivery_id")
            or not ack.get("received_at")
        ):
            return "retry"
        with connection:
            connection.execute(
                "DELETE FROM source_outbox WHERE event_id=?", (event_id,)
            )
        return "delivered"
    # 409 requires fixing the source identity/content; 410 must never trigger
    # automatic identity regeneration. Keep a quarantined row for the operator.
    with connection:
        connection.execute(
            "UPDATE source_outbox SET state='rejected',error_status=? WHERE event_id=?",
            (response.status_code, event_id),
        )
    return "rejected"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        required=True,
        type=Path,
        help="Source-local SQLite file, reused across retries.",
    )
    parser.add_argument(
        "--enqueue",
        type=Path,
        help="JSON upsert envelope. Omit when retrying an already committed item.",
    )
    args = parser.parse_args()
    with sqlite3.connect(args.database) as connection:
        initialize(connection)
        if args.enqueue:
            commit_change(connection, json.loads(args.enqueue.read_text()))
        with httpx.Client(
            base_url=os.environ.get("PAIAGENT_URL", "http://127.0.0.1:8000"),
            headers={
                "Authorization": "Bearer " + os.environ["PAIAGENT_API_KEY"],
                "X-User-ID": os.environ["PAIAGENT_USER_ID"],
            },
            timeout=30,
        ) as client:
            print(deliver_one(connection, client))


if __name__ == "__main__":
    main()
