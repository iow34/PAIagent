"""Authenticated content API; acknowledgement follows the database commit."""

from datetime import datetime
from typing import Any
from uuid import UUID

from litestar import Controller, delete, get, post, put
from litestar.di import NamedDependency, Provide
from litestar.middleware import DefineMiddleware
from litestar.middleware.rate_limit import RateLimitConfig
from litestar.params import JSONBody
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.controllers.integration import (
    PageLimit,
    check_query_parameters,
    contract_error_handler,
    peer_address,
)
from app.api.execution_timeout import ExecutionTimeoutMiddleware
from app.integration.data_types import (
    BatchAck,
    BatchInput,
    EventPage,
    EventView,
    ImportInput,
    IngestEnvelope,
    ItemAck,
    RecordPage,
    RecordView,
)
from app.integration.validation import ContractError
from app.security.authentication import get_access_context
from app.security.context import AccessContext
from app.services.data_service import DataService


def data_service(
    db_session_factory: NamedDependency[async_sessionmaker[AsyncSession]],
    access_context: NamedDependency[AccessContext],
) -> DataService:
    return DataService(db_session_factory, access_context)


class DataController(Controller):
    path = "/v1"
    tags = ["Data lifecycle"]  # noqa: RUF012 - Litestar controller configuration
    security = [{"ServiceAPIKey": []}]  # noqa: RUF012 - Litestar controller configuration
    request_max_body_size = 1048576
    before_request = check_query_parameters
    dependencies = {  # noqa: RUF012 - Litestar controller configuration
        "access_context": Provide(get_access_context),
        "data_service": Provide(data_service, sync_to_thread=False),
    }
    exception_handlers = {ContractError: contract_error_handler}  # noqa: RUF012 - Litestar controller configuration
    middleware = [  # noqa: RUF012 - Litestar controller configuration
        DefineMiddleware(ExecutionTimeoutMiddleware, error_code="data_timeout"),
        RateLimitConfig(
            rate_limit=("minute", 120), identifier_for_request=peer_address
        ).middleware,
    ]

    @post(
        "/collections/{collection_id:uuid}/records/single",
        status_code=200,
        opt={"required_scope": "data:write"},
    )
    async def ingest_record(
        self,
        collection_id: UUID,
        data: JSONBody[IngestEnvelope],
        data_service: NamedDependency[DataService],
    ) -> ItemAck:
        return await data_service.ingest_one(collection_id, data)

    @post(
        "/collections/{collection_id:uuid}/events/single",
        status_code=200,
        opt={"required_scope": "data:write"},
    )
    async def ingest_event(
        self,
        collection_id: UUID,
        data: JSONBody[IngestEnvelope],
        data_service: NamedDependency[DataService],
    ) -> ItemAck:
        return await data_service.ingest_one(collection_id, data, "events")

    @post(
        "/collections/{collection_id:uuid}/records",
        status_code=200,
        opt={"required_scope": "data:write"},
    )
    async def ingest_records(
        self,
        collection_id: UUID,
        data: JSONBody[BatchInput],
        data_service: NamedDependency[DataService],
    ) -> BatchAck:
        return await data_service.batch(collection_id, data, "records")

    @post(
        "/collections/{collection_id:uuid}/events",
        status_code=200,
        opt={"required_scope": "data:write"},
    )
    async def ingest_events(
        self,
        collection_id: UUID,
        data: JSONBody[BatchInput],
        data_service: NamedDependency[DataService],
    ) -> BatchAck:
        return await data_service.batch(collection_id, data, "events")

    @get(
        "/collections/{collection_id:uuid}/records",
        opt={
            "required_scope": "data:read",
            "query_parameters": ("cursor", "limit", "external_id"),
        },
    )
    async def records(
        self,
        collection_id: UUID,
        data_service: NamedDependency[DataService],
        cursor: UUID | None = None,
        limit: PageLimit = 50,
        external_id: str | None = None,
    ) -> RecordPage:
        return await data_service.records(collection_id, cursor, limit, external_id)

    @get("/records/{object_id:uuid}", opt={"required_scope": "data:read"})
    async def record(
        self, object_id: UUID, data_service: NamedDependency[DataService]
    ) -> RecordView:
        return await data_service.record(object_id)

    @put("/records/{object_id:uuid}", opt={"required_scope": "data:write"})
    async def replace_record(
        self,
        object_id: UUID,
        data: JSONBody[IngestEnvelope],
        data_service: NamedDependency[DataService],
    ) -> ItemAck:
        return await data_service.replace_record(object_id, data)

    @post(
        "/events/{delivery_id:uuid}/corrections",
        status_code=200,
        opt={"required_scope": "data:write"},
    )
    async def correct_event(
        self,
        delivery_id: UUID,
        data: JSONBody[IngestEnvelope],
        data_service: NamedDependency[DataService],
    ) -> ItemAck:
        return await data_service.correct_event(delivery_id, data)

    @get(
        "/records/{object_id:uuid}/versions",
        opt={"required_scope": "data:read", "query_parameters": ("cursor", "limit")},
    )
    async def versions(
        self,
        object_id: UUID,
        data_service: NamedDependency[DataService],
        cursor: UUID | None = None,
        limit: PageLimit = 50,
    ) -> EventPage:
        row = await data_service.record(object_id)
        return await data_service.events(
            row.collection_id, cursor, limit, object_id=object_id
        )

    @get(
        "/collections/{collection_id:uuid}/events",
        opt={
            "required_scope": "data:read",
            "query_parameters": (
                "cursor",
                "limit",
                "external_id",
                "occurred_from",
                "occurred_before",
            ),
        },
    )
    async def events(
        self,
        collection_id: UUID,
        data_service: NamedDependency[DataService],
        cursor: UUID | None = None,
        limit: PageLimit = 50,
        external_id: str | None = None,
        occurred_from: datetime | None = None,
        occurred_before: datetime | None = None,
    ) -> EventPage:
        return await data_service.events(
            collection_id, cursor, limit, external_id, occurred_from, occurred_before
        )

    @get("/events/{delivery_id:uuid}", opt={"required_scope": "data:read"})
    async def event(
        self, delivery_id: UUID, data_service: NamedDependency[DataService]
    ) -> EventView:
        return await data_service.event(delivery_id)

    @post(
        "/collections/{collection_id:uuid}/imports",
        status_code=200,
        opt={"required_scope": "data:write"},
    )
    async def import_part(
        self,
        collection_id: UUID,
        data: JSONBody[ImportInput],
        data_service: NamedDependency[DataService],
    ) -> dict[str, Any]:
        """Atomic snapshot part; immutable boundary and part identity; no implicit deletes."""
        return await data_service.import_part(collection_id, data)

    @get("/imports/{import_id:uuid}", opt={"required_scope": "data:read"})
    async def import_status(
        self, import_id: UUID, data_service: NamedDependency[DataService]
    ) -> dict[str, Any]:
        return await data_service.import_status(import_id)

    @get(
        "/imports/{import_id:uuid}/reconciliation",
        opt={"required_scope": "data:read", "query_parameters": ("cursor", "limit")},
    )
    async def reconciliation(
        self,
        import_id: UUID,
        data_service: NamedDependency[DataService],
        cursor: UUID | None = None,
        limit: PageLimit = 50,
    ) -> dict[str, Any]:
        return await data_service.reconcile(import_id, cursor, limit)

    @get(
        "/collections/{collection_id:uuid}/coverage",
        opt={"required_scope": "data:read"},
    )
    async def coverage(
        self, collection_id: UUID, data_service: NamedDependency[DataService]
    ) -> dict[str, Any]:
        return await data_service.coverage(collection_id)

    @delete(
        "/records/{object_id:uuid}",
        status_code=200,
        opt={"required_scope": "data:delete"},
    )
    async def erase_record(
        self, object_id: UUID, data_service: NamedDependency[DataService]
    ) -> dict[str, Any]:
        return await data_service.erase_object(object_id)

    @delete(
        "/events/{delivery_id:uuid}",
        status_code=200,
        opt={"required_scope": "data:delete"},
    )
    async def erase_event(
        self, delivery_id: UUID, data_service: NamedDependency[DataService]
    ) -> dict[str, Any]:
        return await data_service.erase_event(delivery_id)

    @delete(
        "/collections/{collection_id:uuid}/data",
        status_code=200,
        opt={"required_scope": "data:delete"},
    )
    async def erase_collection(
        self, collection_id: UUID, data_service: NamedDependency[DataService]
    ) -> dict[str, Any]:
        return await data_service.erase_scope(collection_id=collection_id)

    @delete(
        "/sources/{source_id:uuid}/data",
        status_code=200,
        opt={"required_scope": "data:delete"},
    )
    async def erase_source(
        self, source_id: UUID, data_service: NamedDependency[DataService]
    ) -> dict[str, Any]:
        return await data_service.erase_scope(source_id=source_id)

    @delete("/users/me/data", status_code=200, opt={"required_scope": "data:delete"})
    async def erase_user(
        self, data_service: NamedDependency[DataService]
    ) -> dict[str, Any]:
        return await data_service.erase_scope()
