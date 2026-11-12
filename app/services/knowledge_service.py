import asyncio
from uuid import UUID

import msgspec
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.knowledge_types import DocumentKnowledge
from app.repositories.knowledge import KnowledgeRepository
from app.security.context import AccessContext
from app.services.knowledge_context import prepare_knowledge_context
from app.services.knowledge_generation import KnowledgeGenerationService
from app.services.knowledge_validation import validate_knowledge

KNOWLEDGE_TIMEOUT_SECONDS = 60.0


class KnowledgeStorageError(RuntimeError):
    pass


class KnowledgeService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        generation_service: KnowledgeGenerationService,
    ) -> None:
        self._session_factory = session_factory
        self._generation_service = generation_service

    async def get_saved(
        self, *, context: AccessContext, document_id: UUID
    ) -> DocumentKnowledge:
        async with asyncio.timeout(KNOWLEDGE_TIMEOUT_SECONDS):
            async with self._session_factory.begin() as session:
                record = await KnowledgeRepository(session).get_saved(
                    context=context, document_id=document_id
                )
                try:
                    return msgspec.convert(
                        {
                            "document_id": record.document_id,
                            "summary": record.summary,
                            "summary_chunk_ids": record.summary_chunk_ids,
                            "topics": record.topics,
                            "facts": record.facts,
                            "sources": record.sources,
                            "coverage": record.coverage,
                        },
                        type=DocumentKnowledge,
                        strict=False,
                    )
                except msgspec.ValidationError as error:
                    raise KnowledgeStorageError(
                        "Stored knowledge has an invalid structure."
                    ) from error

    async def analyze(
        self,
        *,
        context: AccessContext,
        document_id: UUID,
    ) -> DocumentKnowledge:
        async with asyncio.timeout(KNOWLEDGE_TIMEOUT_SECONDS):
            async with self._session_factory.begin() as read_session:
                repository = KnowledgeRepository(read_session)

                chunks = await repository.list_document_chunks(
                    context=context,
                    document_id=document_id,
                )

                prepared_context = prepare_knowledge_context(
                    document_id=document_id,
                    chunks=chunks,
                )

            generated = await self._generation_service.generate(
                context=prepared_context,
            )

            knowledge = validate_knowledge(
                generated=generated,
                context=prepared_context,
            )

            async with self._session_factory.begin() as write_session:
                repository = KnowledgeRepository(write_session)

                await repository.save(
                    context=context,
                    knowledge=knowledge,
                    generation_model=self._generation_service.model,
                )

            return knowledge
