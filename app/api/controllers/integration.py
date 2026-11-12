"""HTTP registry and dry-run validation; ingestion starts in step 7."""

import json
from importlib.resources import files
from typing import Annotated, Any
from uuid import UUID

from litestar import Controller, Request, Response, get, post
from litestar.di import NamedDependency, Provide
from litestar.exceptions import ValidationException
from litestar.middleware.rate_limit import RateLimitConfig
from litestar.openapi.datastructures import ResponseSpec
from litestar.params import JSONBody, Parameter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.integration.catalog import REQUEST_BYTES, REQUESTS_PER_MINUTE, catalog
from app.integration.types import (
    CollectionPage,
    CollectionView,
    Envelope,
    Kind,
    NamedInput,
    SchemaInput,
    SchemaPage,
    SchemaView,
    SourcePage,
    SourceView,
    ValidationResult,
)
from app.integration.validation import ContractError
from app.security.authentication import get_access_context
from app.security.context import AccessContext
from app.services.integration_service import IntegrationService

PageLimit = Annotated[int, Parameter(ge=1, le=100)]
SchemaVersion = Annotated[int, Parameter(ge=1, le=2147483647)]


def contract_error_handler(
    request: Request[Any, Any, Any], error: ContractError
) -> Response[dict[str, Any]]:
    return Response(
        status_code=422,
        content={
            "status_code": 422,
            "detail": str(error),
            "extra": {"code": error.code, "issues": error.issues},
        },
    )


def peer_address(request: Request[Any, Any, Any]) -> str:
    return request.client.host if request.client else "unknown"


def check_query_parameters(request: Request[Any, Any, Any]) -> None:
    allowed = request.route_handler.opt.get("query_parameters", ())
    unknown = set(request.query_params) - set(allowed)
    if unknown or any(
        len(request.query_params.getall(key)) != 1 for key in request.query_params
    ):
        raise ValidationException(
            detail="Unknown or repeated query parameter.",
            extra={"code": "invalid_query"},
        )


def integration_service(
    db_session_factory: NamedDependency[async_sessionmaker[AsyncSession]],
    access_context: NamedDependency[AccessContext],
) -> IntegrationService:
    return IntegrationService(db_session_factory, access_context)


class IntegrationController(Controller):
    path = "/v1"
    tags = ["Integration registry"]  # noqa: RUF012 - Litestar controller configuration
    security = [{"ServiceAPIKey": []}]  # noqa: RUF012 - Litestar controller configuration
    request_max_body_size = REQUEST_BYTES
    before_request = check_query_parameters
    dependencies = {  # noqa: RUF012 - Litestar controller configuration
        "access_context": Provide(get_access_context),
        "integration_service": Provide(integration_service, sync_to_thread=False),
    }
    exception_handlers = {ContractError: contract_error_handler}  # noqa: RUF012 - Litestar controller configuration
    middleware = [  # noqa: RUF012 - Litestar controller configuration
        RateLimitConfig(
            rate_limit=("minute", REQUESTS_PER_MINUTE),
            identifier_for_request=peer_address,
        ).middleware
    ]

    @get("/capabilities", opt={"required_scope": "catalog:read"})
    async def capabilities(
        self, access_context: NamedDependency[AccessContext]
    ) -> dict[str, Any]:
        """Current availability, limits and the caller's grants (not an AI dispatcher)."""
        return catalog(access_context)

    @get("/integration-contract", opt={"required_scope": "catalog:read"})
    async def contract(
        self, access_context: NamedDependency[AccessContext]
    ) -> dict[str, Any]:
        """Versioned design for subsequent steps; planned routes are not executable."""
        return json.loads(
            files("app.integration").joinpath("contract.json").read_text()
        )

    @post("/sources", opt={"required_scope": "sources:write"})
    async def create_source(
        self,
        data: JSONBody[NamedInput],
        integration_service: NamedDependency[IntegrationService],
    ) -> SourceView:
        return await integration_service.create_source(data)

    @get(
        "/sources",
        opt={"required_scope": "sources:read", "query_parameters": ("limit", "cursor")},
    )
    async def sources(
        self,
        integration_service: NamedDependency[IntegrationService],
        limit: PageLimit = 50,
        cursor: UUID | None = None,
    ) -> SourcePage:
        return await integration_service.sources(cursor, limit)

    @get("/sources/{source_id:uuid}", opt={"required_scope": "sources:read"})
    async def source(
        self, source_id: UUID, integration_service: NamedDependency[IntegrationService]
    ) -> SourceView:
        return await integration_service.get_source(source_id)

    @post(
        "/sources/{source_id:uuid}/collections",
        opt={"required_scope": "collections:write"},
    )
    async def create_collection(
        self,
        source_id: UUID,
        data: JSONBody[NamedInput],
        integration_service: NamedDependency[IntegrationService],
    ) -> CollectionView:
        return await integration_service.create_collection(source_id, data)

    @get(
        "/sources/{source_id:uuid}/collections",
        opt={
            "required_scope": "collections:read",
            "query_parameters": ("limit", "cursor"),
        },
    )
    async def collections(
        self,
        source_id: UUID,
        integration_service: NamedDependency[IntegrationService],
        limit: PageLimit = 50,
        cursor: UUID | None = None,
    ) -> CollectionPage:
        return await integration_service.collections(source_id, cursor, limit)

    @get(
        "/collections/{collection_id:uuid}", opt={"required_scope": "collections:read"}
    )
    async def collection(
        self,
        collection_id: UUID,
        integration_service: NamedDependency[IntegrationService],
    ) -> CollectionView:
        return await integration_service.get_collection(collection_id)

    @post(
        "/collections/{collection_id:uuid}/schemas",
        opt={"required_scope": "schemas:write"},
        responses={
            422: ResponseSpec(
                data_container=dict,
                description="Unsupported schema, constraints or field mappings",
            ),
            409: ResponseSpec(
                data_container=dict,
                description="Immutable schema version already exists",
            ),
        },
    )
    async def create_schema(
        self,
        collection_id: UUID,
        data: JSONBody[SchemaInput],
        integration_service: NamedDependency[IntegrationService],
    ) -> SchemaView:
        """Register an immutable (collection, kind, version); existing versions never change."""
        return await integration_service.create_schema(collection_id, data)

    @get(
        "/collections/{collection_id:uuid}/schemas",
        opt={
            "required_scope": "schemas:read",
            "query_parameters": ("limit", "cursor", "kind"),
        },
    )
    async def schemas(
        self,
        collection_id: UUID,
        integration_service: NamedDependency[IntegrationService],
        kind: Kind | None = None,
        limit: PageLimit = 50,
        cursor: UUID | None = None,
    ) -> SchemaPage:
        return await integration_service.schemas(collection_id, kind, cursor, limit)

    @get(
        "/collections/{collection_id:uuid}/schemas/{kind:str}/{version:int}",
        opt={"required_scope": "schemas:read"},
    )
    async def schema(
        self,
        collection_id: UUID,
        kind: Kind,
        version: SchemaVersion,
        integration_service: NamedDependency[IntegrationService],
    ) -> SchemaView:
        return await integration_service.get_schema(collection_id, kind, version)

    @post(
        "/collections/{collection_id:uuid}/validate",
        status_code=200,
        opt={"required_scope": "data:validate"},
        responses={
            422: ResponseSpec(
                data_container=dict,
                description="Envelope or registered schema constraint failed",
            )
        },
    )
    async def validate(
        self,
        collection_id: UUID,
        data: JSONBody[Envelope],
        integration_service: NamedDependency[IntegrationService],
    ) -> ValidationResult:
        """Validate only: does not persist a record/event, assign received_at, or call AI."""
        return await integration_service.validate(collection_id, data)
