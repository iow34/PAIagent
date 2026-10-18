import uuid

import msgspec
from litestar.datastructures import UploadFile

from app.ingestion.chunking import chunk_text
from app.ingestion.parsers.registry import get_parser
from app.repositories.documents import DocumentRepository


class IngestResult(msgspec.Struct):
    document_id: uuid.UUID
    chunks_created: int


class DocumentService:
    def __init__(self, repository: DocumentRepository) -> None:
        self._repository = repository

    async def ingest(self, *, user_id: str, upload: UploadFile) -> IngestResult:
        raw_bytes = await upload.read()
        parser = get_parser(upload.filename)
        text = parser.parse(raw_bytes)

        extension = upload.filename.rsplit(".", 1)[-1].lower()
        document = await self._repository.create(
            user_id=user_id, title=upload.filename, content_type=extension,
        )

        chunks = chunk_text(text)
        await self._repository.add_chunks(document_id=document.id, texts=chunks)
        await self._repository.mark_ready(document)

        return IngestResult(document_id=document.id, chunks_created=len(chunks))