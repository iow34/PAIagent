import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EMBEDDING_DIM, Document, DocumentChunk
from app.security.context import AccessContext


class DocumentReindexConflictError(ValueError):
    """Document is not in a state that allows reindexing."""


class InvalidStoredChunksError(ValueError):
    """Stored fragments cannot be safely reindexed."""


class IndexingAttemptConflictError(ValueError):
    """The attempt no longer owns an active indexing operation."""


INDEXING_LEASE_SECONDS = 180


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        context: AccessContext,
        title: str,
        content_type: str,
        attempt_id: uuid.UUID,
    ) -> Document:
        document = Document(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            title=title,
            content_type=content_type,
            status="processing",
            indexing_attempt_id=attempt_id,
            indexing_expires_at=datetime.now(UTC)
            + timedelta(seconds=INDEXING_LEASE_SECONDS),
        )
        self._session.add(document)
        await self._session.flush()
        return document

    async def add_chunks(self, *, document_id: uuid.UUID, texts: list[str]) -> None:
        chunks = [
            DocumentChunk(document_id=document_id, chunk_index=index, content=text)
            for index, text in enumerate(texts)
        ]
        self._session.add_all(chunks)
        await self._session.flush()

    async def claim_for_reindex(
        self,
        *,
        context: AccessContext,
        document_id: uuid.UUID,
        attempt_id: uuid.UUID,
    ) -> list[str]:
        statement = (
            select(Document)
            .where(
                Document.id == document_id,
                Document.tenant_id == context.tenant_id,
                Document.user_id == context.user_id,
            )
            .with_for_update()
        )
        document = await self._session.scalar(statement)

        if document is None:
            raise LookupError("Document is unavailable.")

        can_reindex = document.status in ("pending", "failed")

        if document.status == "processing":
            expired_document_id = await self._session.scalar(
                select(Document.id).where(
                    Document.id == document.id,
                    Document.indexing_attempt_id.is_not(None),
                    Document.indexing_expires_at <= datetime.now(UTC),
                )
            )
            can_reindex = expired_document_id is not None

        if not can_reindex:
            raise DocumentReindexConflictError(
                "Document is not available for reindexing."
            )

        result = await self._session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document.id)
            .order_by(DocumentChunk.chunk_index)
        )
        chunks = list(result)

        if not chunks:
            raise InvalidStoredChunksError("Document has no chunks.")

        if [chunk.chunk_index for chunk in chunks] != list(range(len(chunks))):
            raise InvalidStoredChunksError("Unexpected chunk indices.")

        if any(not chunk.content.strip() for chunk in chunks):
            raise InvalidStoredChunksError("A document chunk is empty.")

        texts = [chunk.content for chunk in chunks]

        document.status = "processing"
        document.indexing_attempt_id = attempt_id
        document.indexing_expires_at = datetime.now(UTC) + timedelta(
            seconds=INDEXING_LEASE_SECONDS
        )
        document.indexing_error_code = None

        await self._session.flush()
        return texts

    async def save_embeddings(
        self,
        *,
        context: AccessContext,
        document_id: uuid.UUID,
        vectors: list[list[float]],
        model: str,
        dimensions: int,
        total_tokens: int,
        attempt_id: uuid.UUID,
    ) -> None:
        statement = (
            select(Document)
            .where(
                Document.id == document_id,
                Document.tenant_id == context.tenant_id,
                Document.user_id == context.user_id,
            )
            .with_for_update()
        )
        document = await self._session.scalar(statement)
        if document is None:
            raise LookupError("Document is unavailable.")
        if (
            document.status != "processing"
            or document.indexing_attempt_id != attempt_id
        ):
            raise IndexingAttemptConflictError("Indexing attempt is no longer active.")

        # The parent document was authorized and locked in this transaction.
        result = await self._session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document.id)
            .order_by(DocumentChunk.chunk_index)
        )
        chunks = list(result)
        if not chunks or len(chunks) != len(vectors):
            raise ValueError("Chunk and embedding counts do not match.")
        if [chunk.chunk_index for chunk in chunks] != list(range(len(chunks))):
            raise ValueError("Unexpected chunk indices.")
        if dimensions != EMBEDDING_DIM:
            raise ValueError("Embedding dimensions do not match the database.")

        for chunk, vector in zip(chunks, vectors, strict=True):
            chunk.embedding = vector

        document.embedding_model = model
        document.embedding_dimensions = dimensions
        document.embedding_tokens = total_tokens
        document.indexed_at = datetime.now(UTC)
        document.indexing_error_code = None
        document.status = "ready"
        document.indexing_expires_at = None
        await self._session.flush()

    async def mark_failed(
        self,
        *,
        context: AccessContext,
        document_id: uuid.UUID,
        error_code: str,
        attempt_id: uuid.UUID,
    ) -> bool:
        statement = (
            update(Document)
            .where(
                Document.id == document_id,
                Document.tenant_id == context.tenant_id,
                Document.user_id == context.user_id,
                Document.status == "processing",
                Document.indexing_attempt_id == attempt_id,
            )
            .values(
                status="failed",
                indexing_error_code=error_code,
                indexing_expires_at=None,
            )
            .returning(Document.id)
        )
        return await self._session.scalar(statement) is not None

    async def recover_legacy_processing(
        self,
        *,
        context: AccessContext,
        document_id: uuid.UUID,
        apply: bool = False,
    ) -> None:
        statement = select(Document).where(
            Document.id == document_id,
            Document.tenant_id == context.tenant_id,
            Document.user_id == context.user_id,
        )
        if apply:
            statement = statement.with_for_update()

        document = await self._session.scalar(statement)
        if document is None:
            raise LookupError("Document is unavailable.")

        missing_attempt_metadata = (
            document.indexing_attempt_id is None or document.indexing_expires_at is None
        )
        if document.status != "processing" or not missing_attempt_metadata:
            raise DocumentReindexConflictError(
                "Document is not eligible for legacy recovery."
            )

        if not apply:
            return

        document.status = "failed"
        document.indexing_error_code = "legacy_indexing_interrupted"
        document.indexing_attempt_id = None
        document.indexing_expires_at = None
        await self._session.flush()
