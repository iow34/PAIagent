"""Source outbox recovery without any external server or credentials."""

import json
import sqlite3
import unittest

import httpx

from app.integration.delivery_example import commit_change, deliver_one, initialize


class SourceOutboxTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        initialize(self.db)
        self.envelope = {
            "operation": "upsert",
            "collection_id": "collection",
            "external_id": "object",
            "object_version": 1,
            "event_id": "e1",
            "payload": {"text": "private"},
        }
        commit_change(self.db, self.envelope)

    def test_duplicate_event_aborts_source_state_update(self):
        with self.assertRaises(sqlite3.IntegrityError):
            commit_change(
                self.db,
                {**self.envelope, "object_version": 2, "payload": {"text": "changed"}},
            )
        version, payload = self.db.execute(
            "SELECT version,payload FROM source_objects"
        ).fetchone()
        self.assertEqual(version, 1)
        self.assertEqual(json.loads(payload), self.envelope["payload"])

    def test_transport_failure_retains_exact_delivery_for_retry(self):
        attempts = []

        def handler(request):
            attempts.append(request.content)
            if len(attempts) == 1:
                raise httpx.ReadTimeout("lost acknowledgement", request=request)
            return httpx.Response(
                200,
                json={
                    "status": "duplicate",
                    "delivery_id": "receipt",
                    "received_at": "2026-11-13T00:00:00Z",
                },
            )

        with httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://test"
        ) as client:
            self.assertEqual(deliver_one(self.db, client), "retry")
            self.assertEqual(deliver_one(self.db, client), "delivered")
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM source_outbox").fetchone()[0], 0
        )

    def test_permanent_erasure_is_quarantined_without_new_identity(self):
        with httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(410)),
            base_url="http://test",
        ) as client:
            self.assertEqual(deliver_one(self.db, client), "rejected")
            self.assertEqual(deliver_one(self.db, client), "empty")
        event_id, state, error = self.db.execute(
            "SELECT event_id,state,error_status FROM source_outbox"
        ).fetchone()
        self.assertEqual((event_id, state, error), ("e1", "rejected", 410))

    def test_malformed_ack_and_temporary_errors_do_not_discard_data(self):
        for response in (
            httpx.Response(200, json={"status": "accepted"}),
            httpx.Response(200, text="not json"),
            httpx.Response(429),
            httpx.Response(503),
        ):
            with (
                self.subTest(response=response.status_code),
                httpx.Client(
                    transport=httpx.MockTransport(
                        lambda request, result=response: result
                    ),
                    base_url="http://test",
                ) as client,
            ):
                self.assertEqual(deliver_one(self.db, client), "retry")
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM source_outbox WHERE state='pending'"
            ).fetchone()[0],
            1,
        )
