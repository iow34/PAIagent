"""Step 8: multipart format input and shared evidence APIs."""

from typing import Annotated, Any
from uuid import UUID

import msgspec
from litestar import Controller, get, post
from litestar.datastructures import UploadFile
from litestar.di import NamedDependency, Provide
from litestar.enums import RequestEncodingType
from litestar.middleware import DefineMiddleware
from litestar.params import Body, JSONBody
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.controllers.integration import (
    PageLimit,
    check_query_parameters,
    contract_error_handler,
)
from app.api.execution_timeout import ExecutionTimeoutMiddleware
from app.integration.data_types import IngestEnvelope
from app.integration.validation import ContractError
from app.normalization.adapters import MAX_BYTES
from app.normalization.csv_adapter import CSVConfig, rows
from app.security.authentication import get_access_context
from app.security.context import AccessContext
from app.security.permissions import require_scope
from app.services.embedding_service import EmbeddingService
from app.services.evidence_service import EvidenceService


def evidence_service(
    db_session_factory: NamedDependency[async_sessionmaker[AsyncSession]],
    access_context: NamedDependency[AccessContext],
) -> EvidenceService:
    return EvidenceService(db_session_factory, access_context)


class EvidenceQuery(msgspec.Struct, forbid_unknown_fields=True):
    query: str
    limit: Annotated[int, msgspec.Meta(ge=1, le=20)] = 5


class EvidenceController(Controller):
    path = "/v1"
    tags = ["Normalized context"]  # noqa: RUF012 - Litestar configuration
    security = [{"ServiceAPIKey": []}]  # noqa: RUF012 - Litestar configuration
    request_max_body_size = 6 * 1024 * 1024
    before_request = check_query_parameters
    dependencies = {  # noqa: RUF012 - Litestar configuration
        "access_context": Provide(get_access_context),
        "evidence_service": Provide(evidence_service, sync_to_thread=False),
    }
    exception_handlers = {ContractError: contract_error_handler}  # noqa: RUF012 - Litestar configuration
    middleware = [  # noqa: RUF012 - Litestar configuration
        DefineMiddleware(ExecutionTimeoutMiddleware, error_code="normalization_timeout")
    ]

    @post(
        "/collections/{collection_id:uuid}/files",
        status_code=200,
        opt={"required_scope": "data:write"},
    )
    async def file(
        self,
        collection_id: UUID,
        data: Annotated[
            dict[str, Any], Body(media_type=RequestEncodingType.MULTI_PART)
        ],
        evidence_service: NamedDependency[EvidenceService],
    ) -> dict[str, Any]:
        try:
            if set(data) != {"file", "envelope"} or not isinstance(
                data["file"], UploadFile
            ):
                raise ValueError()
            envelope = msgspec.json.decode(data["envelope"], type=IngestEnvelope)
            file = data["file"]
            extension = (file.filename or "").rsplit(".", 1)[-1].lower()
            raw = await file.read(MAX_BYTES + 1)
        except (ValueError, TypeError, msgspec.ValidationError) as error:
            raise ContractError("Multipart file + JSON envelope required.") from error
        return await evidence_service.attach(collection_id, envelope, raw, extension)

    @post(
        "/collections/{collection_id:uuid}/csv",
        status_code=200,
        opt={"required_scope": "data:write"},
    )
    async def csv(
        self,
        collection_id: UUID,
        data: Annotated[
            dict[str, Any], Body(media_type=RequestEncodingType.MULTI_PART)
        ],
        evidence_service: NamedDependency[EvidenceService],
    ) -> dict[str, Any]:
        require_scope(evidence_service.context, "data:write")
        try:
            if set(data) != {"file", "envelope", "mapping"} or not isinstance(
                data["file"], UploadFile
            ):
                raise ValueError()
            envelope = msgspec.json.decode(data["envelope"], type=IngestEnvelope)
            config = msgspec.json.decode(data["mapping"], type=CSVConfig)
            if envelope.operation != "snapshot":
                raise ValueError()
            raw = await data["file"].read(MAX_BYTES + 1)
        except (ValueError, TypeError, msgspec.ValidationError) as error:
            raise ContractError(
                "CSV requires file, snapshot envelope and mapping JSON."
            ) from error
        async with evidence_service.transaction() as repo:
            await evidence_service.collection(repo, collection_id, writable=True)
            from app.repositories.integration import IntegrationRepository

            schema = await IntegrationRepository(
                repo.session, evidence_service.context
            ).schema(collection_id, "record", envelope.schema_version)
            if schema is None:
                raise ContractError("Record schema not found.")
            quantities = {
                m["path"]: m.get("unit")
                for m in schema.field_mappings
                if m["role"] == "quantity"
            }
            for column in config.columns:
                if (
                    "/" + column.field in quantities
                    and column.unit != quantities["/" + column.field]
                ):
                    raise ContractError(
                        "CSV quantity unit must match the registered field mapping."
                    )
        converted = rows(raw, config)
        output = []
        for row in converted:
            if "error" in row:
                output.append(row)
                continue
            item = msgspec.structs.replace(
                envelope,
                external_id=row["external_id"],
                object_version=row["object_version"],
                occurred_at=config.occurred_at,
                payload=row["payload"],
            )
            try:
                ack = await evidence_service.ingest_one(collection_id, item)
                output.append({"line": row["line"], "ack": msgspec.to_builtins(ack)})
            except ContractError as error:
                output.append(
                    {
                        "line": row["line"],
                        "error": {"code": error.code, "detail": str(error)},
                    }
                )
            except Exception as error:
                from litestar.exceptions import HTTPException

                if not isinstance(error, HTTPException):
                    raise
                output.append(
                    {
                        "line": row["line"],
                        "error": {
                            "status_code": error.status_code,
                            "detail": error.detail,
                        },
                    }
                )
        return {
            "items": output,
            "history_complete": False,
            "missing_rows_deleted": False,
        }

    @get(
        "/collections/{collection_id:uuid}/context",
        opt={"required_scope": "data:read", "query_parameters": ("cursor", "limit")},
    )
    async def context(
        self,
        collection_id: UUID,
        evidence_service: NamedDependency[EvidenceService],
        cursor: UUID | None = None,
        limit: PageLimit = 50,
    ) -> dict[str, Any]:
        return await evidence_service.context_page(collection_id, cursor, limit)

    @get("/evidence/{fragment_id:uuid}", opt={"required_scope": "data:read"})
    async def evidence(
        self, fragment_id: UUID, evidence_service: NamedDependency[EvidenceService]
    ) -> dict[str, Any]:
        return await evidence_service.get_evidence(fragment_id)

    @post(
        "/collections/{collection_id:uuid}/index",
        status_code=200,
        opt={"required_scope": "data:write"},
    )
    async def index(
        self,
        collection_id: UUID,
        evidence_service: NamedDependency[EvidenceService],
        embedding_service: NamedDependency[EmbeddingService],
    ) -> dict[str, Any]:
        return await evidence_service.index(collection_id, embedding_service)

    @post(
        "/collections/{collection_id:uuid}/search",
        status_code=200,
        opt={"required_scope": "data:read"},
    )
    async def search(
        self,
        collection_id: UUID,
        data: JSONBody[EvidenceQuery],
        evidence_service: NamedDependency[EvidenceService],
        embedding_service: NamedDependency[EmbeddingService],
    ) -> dict[str, Any]:
        return await evidence_service.search(
            collection_id, data.query, embedding_service, data.limit
        )

    @post(
        "/collections/{collection_id:uuid}/normalize",
        status_code=200,
        opt={"required_scope": "data:write", "query_parameters": ("cursor", "limit")},
    )
    async def normalize(
        self,
        collection_id: UUID,
        evidence_service: NamedDependency[EvidenceService],
        cursor: UUID | None = None,
        limit: PageLimit = 50,
    ) -> dict[str, Any]:
        return await evidence_service.normalize_existing(collection_id, cursor, limit)
