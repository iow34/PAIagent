import asyncio
import logging
import uuid

import msgspec
from openai import APIError, APIStatusError, APITimeoutError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingestion.chunking import chunk_text
from app.ingestion.validation import parse_uploaded_document
from app.repositories.documents import DocumentRepository, IndexingAttemptConflictError
from app.security.context import AccessContext
from app.services.embedding_service import (
    EmbeddingInputError,
    EmbeddingResponseError,
    EmbeddingService,
)

logger = logging.getLogger(__name__)
FAILURE_STATUS_TIMEOUT_SECONDS = 5.0


class IngestResult(msgspec.Struct):
    document_id: uuid.UUID
    chunks_created: int


class ReindexResult(msgspec.Struct):
    document_id: uuid.UUID
    chunks_indexed: int


class DocumentIndexingError(RuntimeError):
    def __init__(self, document_id: uuid.UUID, code: str) -> None:
        super().__init__("Document indexing failed.")
        self.document_id = document_id
        self.code = code


def _indexing_error_code(error: Exception) -> str:
    if isinstance(error, (TimeoutError, APITimeoutError)):
        return "embedding_timeout"
    if isinstance(error, EmbeddingInputError):
        return "invalid_embedding_input"
    if isinstance(error, EmbeddingResponseError):
        return "invalid_embedding_response"
    if isinstance(error, APIError):
        return "embedding_provider_error"
    if isinstance(error, SQLAlchemyError):
        return "index_storage_error"
    return "indexing_internal_error"


class DocumentService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_service: EmbeddingService,
    ) -> None:
        self._session_factory = session_factory
        self._embedding_service = embedding_service

    async def _try_mark_failed(
        self,
        *,
        context: AccessContext,
        document_id: uuid.UUID,
        attempt_id: uuid.UUID,
        error_code: str,
    ) -> None:
        try:
            async with asyncio.timeout(FAILURE_STATUS_TIMEOUT_SECONDS):
                async with self._session_factory.begin() as session:
                    await DocumentRepository(session).mark_failed(
                        context=context,
                        document_id=document_id,
                        attempt_id=attempt_id,
                        error_code=error_code,
                    )
        except Exception as error:
            # Keep the original indexing error; do not log SQL or private text.
            logger.error(
                "Could not mark document %s failed (%s).",
                document_id,
                type(error).__name__,
            )

    async def ingest(
        self,
        *,
        context: AccessContext,
        filename: str | None,
        raw_bytes: bytes,
    ) -> IngestResult:
        parsed = parse_uploaded_document(
            filename=filename,
            raw_bytes=raw_bytes,
        )
        texts = chunk_text(parsed.text)
        attempt_id = uuid.uuid4()

        # Transaction 1: retain the source texts before contacting OpenAI.
        async with self._session_factory.begin() as session:
            repository = DocumentRepository(session)
            document = await repository.create(
                context=context,
                title=parsed.title,
                content_type=parsed.content_type,
                attempt_id=attempt_id,
            )
            await repository.add_chunks(document_id=document.id, texts=texts)
            document_id = document.id

        await self._index_document(
            context=context,
            document_id=document_id,
            attempt_id=attempt_id,
            texts=texts,
        )

        return IngestResult(
            document_id=document_id,
            chunks_created=len(texts),
        )

    async def reindex(
        self,
        *,
        context: AccessContext,
        document_id: uuid.UUID,
    ) -> ReindexResult:
        attempt_id = uuid.uuid4()

        async with self._session_factory.begin() as session:
            texts = await DocumentRepository(session).claim_for_reindex(
                context=context,
                document_id=document_id,
                attempt_id=attempt_id,
            )

        await self._index_document(
            context=context,
            document_id=document_id,
            attempt_id=attempt_id,
            texts=texts,
        )

        return ReindexResult(
            document_id=document_id,
            chunks_indexed=len(texts),
        )

    async def _index_document(
        self,
        *,
        context: AccessContext,
        document_id: uuid.UUID,
        attempt_id: uuid.UUID,
        texts: list[str],
    ) -> None:
        """Index a document after its processing state has been committed."""

        try:
            # There is no open database session or transaction here.
            result = await self._embedding_service.embed_texts(texts)

            # Transaction 2: vectors, metadata and ready are commited together.
            async with self._session_factory.begin() as session:
                await DocumentRepository(session).save_embeddings(
                    context=context,
                    document_id=document_id,
                    attempt_id=attempt_id,
                    vectors=result.vectors,
                    model=result.model,
                    dimensions=result.dimensions,
                    total_tokens=result.total_tokens,
                )
        except IndexingAttemptConflictError as error:
            raise DocumentIndexingError(
                document_id, "indexing_attempt_conflict"
            ) from error
        except asyncio.CancelledError:
            await self._try_mark_failed(
                context=context,
                document_id=document_id,
                attempt_id=attempt_id,
                error_code="indexing_cancelled",
            )
            raise
        except Exception as error:
            code = _indexing_error_code(error)
            await self._try_mark_failed(
                context=context,
                document_id=document_id,
                attempt_id=attempt_id,
                error_code=code,
            )
            logger.error(
                "Indexing failed: document_id=%s code=%s "
                "exception=%s upstream_status=%s",
                document_id,
                code,
                type(error).__name__,
                error.status_code if isinstance(error, APIStatusError) else None,
            )
            raise DocumentIndexingError(document_id, code) from error
