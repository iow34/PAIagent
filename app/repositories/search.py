import math
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EMBEDDING_DIM, Document, DocumentChunk
from app.search_types import SearchHit
from app.security.context import AccessContext

MAX_SEARCH_RESULTS = 100
TEXT_SEARCH_CONFIG = "simple"


class SearchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search_vector(
        self,
        *,
        context: AccessContext,
        query_vector: list[float],
        model: str,
        limit: int = 5,
        document_id: UUID | None = None,
    ) -> list[SearchHit]:
        if not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise ValueError(
                f"Search limit must be between 1 and {MAX_SEARCH_RESULTS}."
            )

        if len(query_vector) != EMBEDDING_DIM:
            raise ValueError("Unexpected query vector dimensions.")

        if not all(math.isfinite(value) for value in query_vector):
            raise ValueError("Query vector contains non-finite values.")

        if not any(query_vector):
            raise ValueError("Query vector must not be a zero vector.")

        distance = DocumentChunk.embedding.cosine_distance(query_vector)

        statement = (
            select(
                DocumentChunk.id.label("chunk_id"),
                DocumentChunk.document_id,
                DocumentChunk.chunk_index,
                DocumentChunk.content,
                (1.0 - distance).label("score"),
            )
            .select_from(DocumentChunk)
            .join(
                Document,
                Document.id == DocumentChunk.document_id,
            )
            .where(
                Document.tenant_id == context.tenant_id,
                Document.user_id == context.user_id,
                Document.status == "ready",
                Document.embedding_model == model,
                Document.embedding_dimensions == EMBEDDING_DIM,
                DocumentChunk.embedding.is_not(None),
            )
        )

        if document_id is not None:
            statement = statement.where(Document.id == document_id)

        statement = statement.order_by(
            distance.asc(),
            DocumentChunk.id.asc(),
        ).limit(limit)

        result = await self._session.execute(statement)

        return [
            SearchHit(
                chunk_id=row.chunk_id,
                document_id=row.document_id,
                chunk_index=row.chunk_index,
                content=row.content,
                score=float(row.score),
            )
            for row in result
        ]

    async def search_full_text(
        self,
        *,
        context: AccessContext,
        query: str,
        model: str,
        limit: int = 5,
        document_id: UUID | None = None,
    ) -> list[SearchHit]:
        if not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise ValueError(
                f"Search limit must be between 1 and {MAX_SEARCH_RESULTS}."
            )

        query = query.strip()

        if not query or "\x00" in query:
            raise ValueError("Search query must be non-empty and contain no NUL.")

        text_vector = func.to_tsvector(
            TEXT_SEARCH_CONFIG,
            DocumentChunk.content,
        )
        text_query = func.plainto_tsquery(
            TEXT_SEARCH_CONFIG,
            query,
        )
        rank = func.ts_rank_cd(text_vector, text_query)

        statement = (
            select(
                DocumentChunk.id.label("chunk_id"),
                DocumentChunk.document_id,
                DocumentChunk.chunk_index,
                DocumentChunk.content,
                rank.label("score"),
            )
            .select_from(DocumentChunk)
            .join(
                Document,
                Document.id == DocumentChunk.document_id,
            )
            .where(
                Document.tenant_id == context.tenant_id,
                Document.user_id == context.user_id,
                Document.status == "ready",
                Document.embedding_model == model,
                Document.embedding_dimensions == EMBEDDING_DIM,
                DocumentChunk.embedding.is_not(None),
                text_vector.bool_op("@@")(text_query),
            )
        )

        if document_id is not None:
            statement = statement.where(Document.id == document_id)

        statement = statement.order_by(
            rank.desc(),
            DocumentChunk.id.asc(),
        ).limit(limit)

        result = await self._session.execute(statement)

        return [
            SearchHit(
                chunk_id=row.chunk_id,
                document_id=row.document_id,
                chunk_index=row.chunk_index,
                content=row.content,
                score=float(row.score),
            )
            for row in result
        ]
