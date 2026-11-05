from uuid import UUID

from app.search_types import SearchHit

RRF_K = 60


def reciprocal_rank_fusion(
    *,
    vector_hits: list[SearchHit],
    full_text_hits: list[SearchHit],
    limit: int,
) -> list[SearchHit]:
    if limit < 1:
        raise ValueError("Result limit must be greater than zero.")

    hits_by_id: dict[UUID, SearchHit] = {}
    scores: dict[UUID, float] = {}

    for ranking in (vector_hits, full_text_hits):
        for position, hit in enumerate(ranking, start=1):
            chunk_id = hit.chunk_id

            hits_by_id[chunk_id] = hit
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + position)

    ranked_ids = sorted(
        scores,
        key=lambda chunk_id: (
            -scores[chunk_id],
            chunk_id.int,
        ),
    )

    merged: list[SearchHit] = []

    for chunk_id in ranked_ids[:limit]:
        hit = hits_by_id[chunk_id]

        merged.append(
            SearchHit(
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                chunk_index=hit.chunk_index,
                content=hit.content,
                score=scores[chunk_id],
            )
        )

    return merged
