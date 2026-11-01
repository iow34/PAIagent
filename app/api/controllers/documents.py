import uuid
from typing import Annotated

from litestar import Controller, post
from litestar.datastructures import UploadFile
from litestar.di import NamedDependency, Provide
from litestar.enums import RequestEncodingType
from litestar.exceptions import HTTPException
from litestar.params import Body
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingestion.validation import (
    MAX_FILE_BYTES,
    MAX_REQUEST_BYTES,
    FileToolLargeError,
    InvalidDocumentError,
    UnsupportedFileTypeError,
)
from app.repositories.documents import (
    DocumentReindexConflictError,
    InvalidStoredChunksError,
)
from app.security.authentication import get_access_context
from app.security.context import AccessContext
from app.services.document_service import (
    DocumentIndexingError,
    DocumentService,
    EmbeddingService,
    IngestResult,
    ReindexResult,
)


def _indexing_http_exception(error: DocumentIndexingError) -> HTTPException:
    status_code = {
        "indexing_attempt_conflict": 409,
        "embedding_timeout": 504,
        "embedding_provider_error": 503,
        "invalid_embedding_response": 502,
        "invalid_embedding_input": 422,
    }.get(error.code, 500)

    return HTTPException(
        status_code=status_code,
        detail="Document indexing failed.",
        extra={
            "document_id": str(error.document_id),
            "code": error.code,
        },
    )


class DocumentController(Controller):
    path = "/v1/documents"

    security = [{"ServiceAPIKey": []}]

    dependencies = {
        "access_context": Provide(get_access_context),
    }

    @post(request_max_body_size=MAX_REQUEST_BYTES)
    async def upload_document(
        self,
        data: Annotated[
            UploadFile,
            Body(media_type=RequestEncodingType.MULTI_PART),
        ],
        db_session_factory: NamedDependency[async_sessionmaker[AsyncSession]],
        access_context: NamedDependency[AccessContext],
        embedding_service: NamedDependency[EmbeddingService],
    ) -> IngestResult:
        service = DocumentService(db_session_factory, embedding_service)

        try:
            raw_bytes = await data.read(MAX_FILE_BYTES + 1)

            return await service.ingest(
                context=access_context,
                filename=data.filename,
                raw_bytes=raw_bytes,
            )
        except UnsupportedFileTypeError as error:
            raise HTTPException(
                status_code=415,
                detail=str(error),
            ) from error
        except FileToolLargeError as error:
            raise HTTPException(
                status_code=413,
                detail=str(error),
            ) from error
        except InvalidDocumentError as error:
            raise HTTPException(
                status_code=400,
                detail=str(error),
            ) from error
        except DocumentIndexingError as error:
            raise _indexing_http_exception(error) from error

    @post("/{document_id:uuid}/reindex", status_code=200)
    async def reindex_document(
        self,
        document_id: uuid.UUID,
        db_session_factory: NamedDependency[async_sessionmaker[AsyncSession]],
        access_context: NamedDependency[AccessContext],
        embedding_service: NamedDependency[EmbeddingService],
    ) -> ReindexResult:
        service = DocumentService(
            db_session_factory, embedding_service=embedding_service
        )

        try:
            return await service.reindex(
                context=access_context,
                document_id=document_id,
            )
        except LookupError as error:
            raise HTTPException(
                status_code=404,
                detail="Document is unavailable.",
            ) from error
        except DocumentReindexConflictError as error:
            raise HTTPException(
                status_code=409,
                detail="Document is not available for reindexing.",
                extra={"code": "document_reindex_conflict"},
            ) from error
        except InvalidStoredChunksError as error:
            raise HTTPException(
                status_code=409,
                detail="Stored document chunks cannot be reindexed.",
                extra={"code": "invalid_stored_chunks"},
            ) from error
        except DocumentIndexingError as error:
            raise _indexing_http_exception(error) from error
