"""Durable run admission, status polling, cancellation and profile triggers."""

from uuid import UUID

import msgspec
from litestar import Controller, Response, get, post, put
from litestar.di import NamedDependency, Provide
from litestar.middleware import DefineMiddleware
from litestar.openapi.datastructures import ResponseSpec
from litestar.params import JSONBody
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.controllers.integration import (
    PageLimit,
    check_query_parameters,
    contract_error_handler,
)
from app.api.execution_timeout import ExecutionTimeoutMiddleware
from app.capabilities.generation import OpenAIGenerator
from app.integration.validation import ContractError
from app.jobs.types import (
    CreateRun,
    RunPage,
    RunView,
    Status,
    TriggerInput,
    TriggerView,
)
from app.security.authentication import get_access_context
from app.security.context import AccessContext
from app.services.embedding_service import EmbeddingService
from app.services.job_service import JobService
from app.services.trigger_service import TriggerService


def job_service(
    db_session_factory: NamedDependency[async_sessionmaker[AsyncSession]],
    access_context: NamedDependency[AccessContext],
) -> JobService:
    return JobService(db_session_factory, access_context)


class JobController(Controller):
    path = "/v1"
    tags = ["Background execution"]  # noqa: RUF012
    security = [{"ServiceAPIKey": []}]  # noqa: RUF012
    request_max_body_size = 1048576
    before_request = check_query_parameters
    dependencies = {  # noqa: RUF012
        "access_context": Provide(get_access_context),
        "job_service": Provide(job_service, sync_to_thread=False),
    }
    exception_handlers = {ContractError: contract_error_handler}  # noqa: RUF012
    middleware = [  # noqa: RUF012 - Litestar configuration
        DefineMiddleware(ExecutionTimeoutMiddleware, error_code="run_timeout")
    ]

    @post(
        "/runs",
        status_code=202,
        opt={"required_scope": "runs:write"},
        responses={
            200: ResponseSpec(
                data_container=RunView, description="Synchronous terminal run"
            )
        },
    )
    async def create(
        self,
        data: JSONBody[CreateRun],
        job_service: NamedDependency[JobService],
        capability_generator: NamedDependency[OpenAIGenerator],
        capability_embeddings: NamedDependency[EmbeddingService],
    ) -> Response[RunView]:
        value = await job_service.enqueue(data)
        if data.mode == "sync" and value["status"] == "pending":
            claim = await job_service.claim(UUID(value["id"]))
            if claim:
                await job_service.execute(
                    claim, capability_generator, capability_embeddings
                )
            value = await job_service.created_view(UUID(value["id"]))
        return Response(
            msgspec.convert(value, type=RunView),
            status_code=200
            if data.mode == "sync"
            and value["status"] in {"succeeded", "failed", "cancelled"}
            else 202,
        )

    @get(
        "/runs",
        opt={
            "required_scope": "runs:read",
            "query_parameters": ("cursor", "limit", "status", "profile_id"),
        },
    )
    async def runs(
        self,
        job_service: NamedDependency[JobService],
        cursor: UUID | None = None,
        limit: PageLimit = 50,
        status: Status | None = None,
        profile_id: UUID | None = None,
    ) -> RunPage:
        return msgspec.convert(
            await job_service.runs(cursor, limit, status, profile_id), type=RunPage
        )

    @get("/runs/{run_id:uuid}", opt={"required_scope": "runs:read"})
    async def run(
        self, run_id: UUID, job_service: NamedDependency[JobService]
    ) -> RunView:
        return msgspec.convert(await job_service.get_run(run_id), type=RunView)

    @post(
        "/runs/{run_id:uuid}/cancel",
        status_code=200,
        opt={"required_scope": "runs:write"},
    )
    async def cancel(
        self, run_id: UUID, job_service: NamedDependency[JobService]
    ) -> RunView:
        return msgspec.convert(await job_service.cancel(run_id), type=RunView)

    @put(
        "/profiles/{profile_id:uuid}/triggers", opt={"required_scope": "profiles:write"}
    )
    async def triggers(
        self,
        profile_id: UUID,
        data: JSONBody[TriggerInput],
        job_service: NamedDependency[JobService],
    ) -> TriggerView:
        service = TriggerService(job_service.factory, job_service.context)
        return msgspec.convert(
            await service.configure(profile_id, data), type=TriggerView
        )

    @get(
        "/profiles/{profile_id:uuid}/triggers", opt={"required_scope": "profiles:read"}
    )
    async def get_triggers(
        self, profile_id: UUID, job_service: NamedDependency[JobService]
    ) -> TriggerView:
        return msgspec.convert(
            await TriggerService(job_service.factory, job_service.context).get_trigger(
                profile_id
            ),
            type=TriggerView,
        )
