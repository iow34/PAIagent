"""Published onboarding in a separate process against real HTTP/PostgreSQL."""

import asyncio
import json
import os
import socket
import sys
import unittest
from uuid import UUID

import httpx
import test_postgres_indexing as fixture
import test_postgres_jobs as job_fixture
import uvicorn
from litestar import Litestar
from litestar.di import Provide
from test_postgres_capabilities import FakeGenerator

from app.api.controllers.capabilities import CapabilityController
from app.api.controllers.data import DataController
from app.api.controllers.evidence import EvidenceController
from app.api.controllers.integration import IntegrationController
from app.api.controllers.jobs import JobController
from app.api.controllers.memory import MemoryController
from app.asgi import create_app
from app.cli.create_tenant import create_tenant
from app.db.models import Tenant
from app.jobs.types import TriggerInput
from app.jobs.worker import serve
from app.services.trigger_service import TriggerService


@unittest.skipUnless(fixture.TEST_URL, "Use temporary PostgreSQL runner")
class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PostgreSQLIndexingTests.asyncSetUp
    drop_schema = fixture.PostgreSQLIndexingTests.drop_schema
    setup_job = job_fixture.JobTests.setup_job
    seed = job_fixture.JobTests.seed

    async def test_tenant_provisioning(self):
        identifier = await create_tenant(self.factory, "  New tenant  ")
        async with self.factory() as session:
            self.assertEqual((await session.get(Tenant, identifier)).name, "New tenant")
        with self.assertRaises(ValueError):
            await create_tenant(self.factory, " ")

    async def test_all_six_public_quickstarts_and_trigger_delivery(self):
        await self.exercise(
            [
                (name, "tasks")
                for name in (
                    "search",
                    "query",
                    "summarize",
                    "extract",
                    "analyze",
                    "recommend",
                )
            ],
            "responses",
        )

    async def test_books_and_learning_session_profiles(self):
        # Independent installation/peer rate window for these additional examples.
        await self.exercise(
            [("search", "books"), ("analyze", "learning_sessions")], "domain-responses"
        )

    async def exercise(self, cases, output):
        service, *_ = await self.setup_job()
        generator = FakeGenerator()
        embeddings = fixture.FakeEmbeddings()

        async def factory():
            return self.factory

        app = Litestar(
            route_handlers=[
                IntegrationController,
                DataController,
                EvidenceController,
                CapabilityController,
                JobController,
                MemoryController,
            ],
            dependencies={
                "db_session_factory": Provide(factory),
                "capability_generator": Provide(
                    lambda: generator, sync_to_thread=False
                ),
                "capability_embeddings": Provide(
                    lambda: embeddings, sync_to_thread=False
                ),
                "embedding_service": Provide(lambda: embeddings, sync_to_thread=False),
            },
            openapi_config=create_app().openapi_config,
        )
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        server_task = asyncio.create_task(server.serve(sockets=[sock]))
        worker = asyncio.create_task(
            serve(self.factory, generator, embeddings, poll_seconds=0.05)
        )
        env = {
            **os.environ,
            "PAIAGENT_URL": url,
            "PAIAGENT_USER_ID": service.context.user_id,
            "PAIAGENT_SERVICE_KEY": "job-test-" + service.context.api_key_id.hex,
        }
        process = None
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            samples = []
            for capability, domain in cases:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "documentation/examples/quickstart.py",
                    "--capability",
                    capability,
                    "--domain",
                    domain,
                    "--mode",
                    "async" if capability == "search" else "sync",
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
                self.assertEqual(process.returncode, 0, stderr.decode())
                value = json.loads(stdout)
                self.assertEqual(value["result"]["capability"], capability)
                self.assertEqual(value["result"]["execution_status"], "succeeded")
                for delivery in value["deliveries"]:
                    self.assertEqual(delivery["ack"]["status"], "accepted")
                    self.assertEqual(delivery["replay"]["status"], "duplicate")
                # Verify actual file normalization, not merely multipart acknowledgement.
                async with httpx.AsyncClient(
                    base_url=url,
                    headers={
                        "Authorization": "Bearer " + env["PAIAGENT_SERVICE_KEY"],
                        "X-User-ID": env["PAIAGENT_USER_ID"],
                    },
                ) as client:
                    context = await client.get(
                        f"/v1/collections/{value['collections']['books']}/context"
                    )
                    self.assertEqual(context.status_code, 200, context.text)
                    self.assertIn("Python", context.text)
                    async with asyncio.timeout(8):
                        while True:
                            runs = (
                                await client.get(
                                    "/v1/runs",
                                    params={"profile_id": value["profile_id"]},
                                )
                            ).json()["items"]
                            changed = [
                                r
                                for r in runs
                                if r["reason"] == "change"
                                and r["status"] == "succeeded"
                            ]
                            if changed:
                                break
                            await asyncio.sleep(0.1)
                    response = await client.get(
                        f"/v1/results/{changed[0]['result_id']}"
                    )
                    self.assertEqual(response.status_code, 200)
                samples.append(
                    {
                        "capability": capability,
                        "domain": domain,
                        "delivery": value["deliveries"][0],
                        "profile": next(
                            p
                            for p in value["profiles"]
                            if p["profile_id"] == value["profile_id"]
                        ),
                        "result": value["result"],
                        "trigger": value["trigger"],
                    }
                )
                if capability == "recommend":
                    memory_env = {
                        **env,
                        "PAIAGENT_COLLECTION_ID": value["collections"]["tasks"],
                        "PAIAGENT_RESULT_ID": value["result"]["result_id"],
                    }
                    process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "documentation/examples/memory_feedback.py",
                        env=memory_env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
                    self.assertEqual(process.returncode, 0, stderr.decode())
                    memory = json.loads(stdout)
                    self.assertEqual(memory["preference"]["state"], "confirmed")
                    self.assertFalse(memory["disabled"]["enabled"])
                    samples.append({"memory_feedback": memory})
                    async with httpx.AsyncClient(
                        base_url=url,
                        headers={
                            "Authorization": "Bearer " + env["PAIAGENT_SERVICE_KEY"],
                            "X-User-ID": env["PAIAGENT_USER_ID"],
                        },
                    ) as client:
                        errors = []
                        for method, path, body, status in [
                            (
                                "POST",
                                f"/v1/collections/{value['collections']['tasks']}/schemas",
                                {
                                    "kind": "record",
                                    "version": 2,
                                    "json_schema": {
                                        "type": "object",
                                        "additionalProperties": True,
                                    },
                                },
                                422,
                            ),
                            (
                                "PUT",
                                "/v1/memory/preferences",
                                {
                                    "collection_id": value["collections"]["tasks"],
                                    "key": "response_format",
                                    "value": "brief",
                                },
                                422,
                            ),
                            (
                                "POST",
                                f"/v1/results/{value['result']['result_id']}/feedback",
                                {
                                    "request_id": memory["feedback"]["request_id"],
                                    "rating": "unhelpful",
                                },
                                409,
                            ),
                        ]:
                            response = await client.request(method, path, json=body)
                            self.assertEqual(
                                response.status_code, status, response.text
                            )
                            errors.append(
                                {
                                    "method": method,
                                    "path": path,
                                    "request": body,
                                    "status": status,
                                    "response": response.json(),
                                }
                            )
                        await TriggerService(self.factory, service.context).configure(
                            UUID(value["profile_id"]),
                            TriggerInput(
                                1,
                                enabled=False,
                                query=value["trigger"]["config"]["query"],
                            ),
                        )
                        obj = value["deliveries"][0]["ack"]["object_id"]
                        deletion = await client.delete(f"/v1/records/{obj}")
                        self.assertEqual(deletion.status_code, 200, deletion.text)
                        missing = await client.get(f"/v1/records/{obj}")
                        self.assertEqual(missing.status_code, 404)
                        samples.append(
                            {
                                "errors": errors,
                                "erasure": {
                                    "method": "DELETE",
                                    "path": f"/v1/records/{obj}",
                                    "status": 200,
                                    "response": deletion.json(),
                                    "subsequent_get_status": 404,
                                },
                            }
                        )
                # Stop each demo's trigger so later cases have isolated admission activity.
                await TriggerService(self.factory, service.context).configure(
                    UUID(value["profile_id"]),
                    TriggerInput(
                        1, enabled=False, query=value["trigger"]["config"]["query"]
                    ),
                )
            from pathlib import Path

            Path(f"/tmp/paiagent-step12-{output}-examples.json").write_text(
                json.dumps(samples, ensure_ascii=False, indent=2) + "\n"
            )
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            server.should_exit = True
            await asyncio.wait_for(server_task, 5)
            sock.close()
