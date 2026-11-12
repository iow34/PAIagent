from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.db.models import (
    Document,
    DocumentChunk,
    DocumentKnowledgeRecord,
)
from app.knowledge_types import DocumentKnowledge
from app.security.context import AccessContext


class KnowledgeDocumentNotFoundError(LookupError):
    pass


class KnowledgeDocumentNotReadyError(ValueError):
    pass


class KnowledgeSourceConflictError(ValueError):
    pass


class KnowledgeNotFoundError(LookupError):
    pass


class KnowledgeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_ready_document(
        self,
        *,
        context: AccessContext,
        document_id: UUID,
        for_update: bool = False,
    ) -> Document:
        statement = select(Document).where(
            Document.id == document_id,
            Document.tenant_id == context.tenant_id,
            Document.user_id == context.user_id,
        )

        if for_update:
            statement = statement.with_for_update()

        document = await self._session.scalar(statement)

        if document is None:
            raise KnowledgeDocumentNotFoundError("Document is unavailable.")

        if document.status != "ready":
            raise KnowledgeDocumentNotReadyError(
                "Knowledge analysis requires a ready document."
            )

        return document

    async def list_document_chunks(
        self,
        *,
        context: AccessContext,
        document_id: UUID,
    ) -> list[DocumentChunk]:
        await self.get_ready_document(
            context=context,
            document_id=document_id,
        )

        statement = (
            select(DocumentChunk)
            .join(
                Document,
                Document.id == DocumentChunk.document_id,
            )
            .where(
                Document.id == document_id,
                Document.tenant_id == context.tenant_id,
                Document.user_id == context.user_id,
                Document.status == "ready",
            )
            .options(
                load_only(
                    DocumentChunk.id,
                    DocumentChunk.document_id,
                    DocumentChunk.chunk_index,
                    DocumentChunk.content,
                    raiseload=True,
                )
            )
            .order_by(DocumentChunk.chunk_index.asc())
        )

        result = await self._session.scalars(statement)

        return list(result)

    async def get_saved(
        self, *, context: AccessContext, document_id: UUID
    ) -> DocumentKnowledgeRecord:
        # Ownership is enforced in the same query that retrieves private content.
        statement = (
            select(DocumentKnowledgeRecord)
            .join(Document, Document.id == DocumentKnowledgeRecord.document_id)
            .where(
                Document.id == document_id,
                Document.tenant_id == context.tenant_id,
                Document.user_id == context.user_id,
                Document.status == "ready",
            )
        )
        record = await self._session.scalar(statement)
        if record is None:
            raise KnowledgeNotFoundError("Document knowledge is unavailable.")
        return record

    async def save(
        self,
        *,
        context: AccessContext,
        knowledge: DocumentKnowledge,
        generation_model: str,
    ) -> None:
        generation_model = generation_model.strip()

        if not generation_model or "\x00" in generation_model:
            raise ValueError("Knowledge generation model is invalid.")

        await self.get_ready_document(
            context=context,
            document_id=knowledge.document_id,
            for_update=True,
        )

        current_chunks = await self.list_document_chunks(
            context=context,
            document_id=knowledge.document_id,
        )

        chunks_by_id = {chunk.id: chunk for chunk in current_chunks}

        if not knowledge.sources:
            raise KnowledgeSourceConflictError("Knowledge has no source fragments.")

        for source in knowledge.sources:
            chunk = chunks_by_id.get(source.chunk_id)

            if chunk is None:
                raise KnowledgeSourceConflictError(
                    "A knowledge source is no longer available."
                )

            if (
                chunk.chunk_index != source.chunk_index
                or chunk.content != source.content
            ):
                raise KnowledgeSourceConflictError("A knowledge source has changed.")

        if knowledge.coverage == "full" and len(current_chunks) != len(
            knowledge.sources
        ):
            raise KnowledgeSourceConflictError(
                "Knowledge no longer covers the complete document."
            )

        serialized_facts: list[dict[str, object]] = [
            {
                "text": fact.text,
                "chunk_ids": [str(chunk_id) for chunk_id in fact.chunk_ids],
            }
            for fact in knowledge.facts
        ]

        serialized_sources: list[dict[str, object]] = [
            {
                "chunk_id": str(source.chunk_id),
                "chunk_index": source.chunk_index,
                "content": source.content,
            }
            for source in knowledge.sources
        ]

        statement = insert(DocumentKnowledgeRecord).values(
            document_id=knowledge.document_id,
            summary=knowledge.summary,
            summary_chunk_ids=[
                str(chunk_id) for chunk_id in knowledge.summary_chunk_ids
            ],
            topics=list(knowledge.topics),
            facts=serialized_facts,
            sources=serialized_sources,
            coverage=knowledge.coverage,
            generation_model=generation_model,
        )

        statement = statement.on_conflict_do_update(
            index_elements=[
                DocumentKnowledgeRecord.document_id,
            ],
            set_={
                "summary": statement.excluded.summary,
                "summary_chunk_ids": (statement.excluded.summary_chunk_ids),
                "topics": statement.excluded.topics,
                "facts": statement.excluded.facts,
                "sources": statement.excluded.sources,
                "coverage": statement.excluded.coverage,
                "generation_model": (statement.excluded.generation_model),
                "updated_at": func.now(),
            },
        )

        await self._session.execute(statement)
