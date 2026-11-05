from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.search import MAX_SEARCH_RESULTS, SearchRepository
from app.search_types import SearchHit
from app.security.context import AccessContext
from app.services.embedding_service import (
    EmbeddingInputError,
    EmbeddingService,
)
from app.services.search_fusion import reciprocal_rank_fusion

MAX_SEARCH_QUERY_CHARS = 2000


class SearchInputError(ValueError):
    """The search request contains invalid input."""


class SearchService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_service: EmbeddingService,
    ) -> None:
        self._session_factory = session_factory
        self._embedding_service = embedding_service

    async def search(
        self,
        *,
        context: AccessContext,
        query: str,
        limit: int = 5,
        document_id: UUID | None = None,
    ) -> list[SearchHit]:
        if len(query) > MAX_SEARCH_QUERY_CHARS:
            raise SearchInputError(
                f"Search query must not exceed {MAX_SEARCH_QUERY_CHARS} characters."
            )

        query = query.strip()

        if not query:
            raise SearchInputError("Search query must not be empty.")

        if "\x00" in query:
            raise SearchInputError("Search query must not contain NUL characters.")

        if not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise SearchInputError(
                f"Search limit must be between 1 and {MAX_SEARCH_RESULTS}."
            )

        try:
            embedding = await self._embedding_service.embed_texts([query])
        except EmbeddingInputError as error:
            raise SearchInputError(str(error)) from error

        candidate_limit = min(
            MAX_SEARCH_RESULTS,
            max(20, limit * 4),
        )

        async with self._session_factory.begin() as session:
            repository = SearchRepository(session)

            vector_hits = await repository.search_vector(
                context=context,
                query_vector=embedding.vectors[0],
                model=embedding.model,
                limit=candidate_limit,
                document_id=document_id,
            )

            full_text_hits = await repository.search_full_text(
                context=context,
                query=query,
                model=embedding.model,
                limit=candidate_limit,
                document_id=document_id,
            )

        return reciprocal_rank_fusion(
            vector_hits=vector_hits,
            full_text_hits=full_text_hits,
            limit=limit,
        )
