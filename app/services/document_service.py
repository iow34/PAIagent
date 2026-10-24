import uuid

import msgspec
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingestion.chunking import chunk_text
from app.ingestion.validation import parse_uploaded_document
from app.repositories.documents import DocumentRepository
from app.security.context import AccessContext


class IngestResult(msgspec.Struct):
    document_id: uuid.UUID
    chunks_created: int


class DocumentService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

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

        chunks = chunk_text(parsed.text)

        async with self._session_factory.begin() as session:
            repository = DocumentRepository(session)

            document = await repository.create(
                context=context,
                title=parsed.title,
                content_type=parsed.content_type,
            )

            await repository.add_chunks(document_id=document.id, texts=chunks)
            await repository.mark_ready(document)

            document_id = document.id

        return IngestResult(document_id=document_id, chunks_created=len(chunks))
