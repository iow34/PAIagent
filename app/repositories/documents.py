import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentChunk


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, user_id: str, title: str, content_type: str) -> Document:
        document = Document(
            user_id=user_id,
            title=title,
            content_type=content_type,
            status="processing",
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

    async def mark_ready(self, document: Document) -> None:
        document.status = "ready"
        await self._session.flush()