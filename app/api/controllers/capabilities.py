"""Declarative profile registry and finite synchronous AI runs."""

from typing import Any
from uuid import UUID

import msgspec
from litestar import Controller, delete, get, post
from litestar.di import NamedDependency, Provide
from litestar.middleware import DefineMiddleware
from litestar.params import JSONBody
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.controllers.integration import (
    PageLimit,
    SchemaVersion,
    check_query_parameters,
    contract_error_handler,
)
from app.api.execution_timeout import ExecutionTimeoutMiddleware
from app.capabilities.generation import OpenAIGenerator
from app.capabilities.types import (
    CapabilityResult,
    ProfileInput,
    ProfilePage,
    ProfileView,
    ResultPage,
    RunInput,
)
from app.integration.validation import ContractError
from app.security.authentication import get_access_context
from app.security.context import AccessContext
from app.services.capability_service import CapabilityService
from app.services.embedding_service import EmbeddingService


def capability_service(
    db_session_factory: NamedDependency[async_sessionmaker[AsyncSession]],
    access_context: NamedDependency[AccessContext],
) -> CapabilityService:
    return CapabilityService(db_session_factory, access_context)


class CapabilityController(Controller):
    path = "/v1"
    tags = ["AI capabilities"]  # noqa: RUF012
    security = [{"ServiceAPIKey": []}]  # noqa: RUF012
    request_max_body_size = 1048576
    before_request = check_query_parameters
    dependencies = {  # noqa: RUF012
        "access_context": Provide(get_access_context),
        "capability_service": Provide(capability_service, sync_to_thread=False),
    }
    exception_handlers = {ContractError: contract_error_handler}  # noqa: RUF012
    middleware = [  # noqa: RUF012 - Litestar configuration
        DefineMiddleware(ExecutionTimeoutMiddleware, error_code="capability_timeout")
    ]

    @post("/profiles", opt={"required_scope": "profiles:write"})
    async def create(
        self,
        data: JSONBody[ProfileInput],
        capability_service: NamedDependency[CapabilityService],
    ) -> ProfileView:
        return msgspec.convert(
            await capability_service.create_profile(data), type=ProfileView
        )

    @get(
        "/profiles",
        opt={
            "required_scope": "profiles:read",
            "query_parameters": ("cursor", "limit"),
        },
    )
    async def profiles(
        self,
        capability_service: NamedDependency[CapabilityService],
        cursor: UUID | None = None,
        limit: PageLimit = 50,
    ) -> ProfilePage:
        return msgspec.convert(
            await capability_service.profiles(cursor, limit), type=ProfilePage
        )

    @get("/profiles/{profile_id:uuid}", opt={"required_scope": "profiles:read"})
    async def profile(
        self, profile_id: UUID, capability_service: NamedDependency[CapabilityService]
    ) -> ProfileView:
        return msgspec.convert(
            await capability_service.get_profile(profile_id), type=ProfileView
        )

    @get(
        "/profiles/{profile_id:uuid}/versions/{version:int}",
        opt={"required_scope": "profiles:read"},
    )
    async def version(
        self,
        profile_id: UUID,
        version: SchemaVersion,
        capability_service: NamedDependency[CapabilityService],
    ) -> ProfileView:
        return msgspec.convert(
            await capability_service.get_profile(profile_id, version), type=ProfileView
        )

    @post(
        "/profiles/{profile_id:uuid}/versions/{version:int}",
        opt={"required_scope": "profiles:write"},
    )
    async def add_version(
        self,
        profile_id: UUID,
        version: SchemaVersion,
        data: JSONBody[ProfileInput],
        capability_service: NamedDependency[CapabilityService],
    ) -> ProfileView:
        return msgspec.convert(
            await capability_service.create_profile(data, profile_id, version),
            type=ProfileView,
        )

    @delete(
        "/profiles/{profile_id:uuid}",
        status_code=200,
        opt={"required_scope": "profiles:write"},
    )
    async def remove(
        self, profile_id: UUID, capability_service: NamedDependency[CapabilityService]
    ) -> dict[str, Any]:
        return await capability_service.delete_profile(profile_id)

    @post(
        "/profiles/{profile_id:uuid}/runs",
        status_code=200,
        opt={"required_scope": "ai:run"},
    )
    async def run(
        self,
        profile_id: UUID,
        data: JSONBody[RunInput],
        capability_service: NamedDependency[CapabilityService],
        capability_generator: NamedDependency[OpenAIGenerator],
        capability_embeddings: NamedDependency[EmbeddingService],
    ) -> CapabilityResult:
        result = await capability_service.run(
            profile_id, data, capability_generator, capability_embeddings
        )

        return msgspec.convert(result, type=CapabilityResult)

    @get("/results/{result_id:uuid}", opt={"required_scope": "results:read"})
    async def result(
        self, result_id: UUID, capability_service: NamedDependency[CapabilityService]
    ) -> CapabilityResult:
        return msgspec.convert(
            await capability_service.result(result_id), type=CapabilityResult
        )

    @get(
        "/profiles/{profile_id:uuid}/results",
        opt={"required_scope": "results:read", "query_parameters": ("cursor", "limit")},
    )
    async def results(
        self,
        profile_id: UUID,
        capability_service: NamedDependency[CapabilityService],
        cursor: UUID | None = None,
        limit: PageLimit = 50,
    ) -> ResultPage:
        return msgspec.convert(
            await capability_service.results(profile_id, cursor, limit), type=ResultPage
        )
