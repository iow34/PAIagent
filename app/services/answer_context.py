import json
from collections.abc import Sequence
from uuid import UUID

import msgspec

from app.answer_types import AnswerSource
from app.search_types import SearchHit

MAX_CONTEXT_SOURCES = 8
MAX_SOURCE_CHARS = 2000
MAX_CONTEXT_CHARS = 12000


class PreparedAnswerContext(msgspec.Struct, frozen=True):
    text: str
    sources: tuple[AnswerSource, ...]


def prepare_answer_context(hits: Sequence[SearchHit]) -> PreparedAnswerContext:
    sources: list[AnswerSource] = []
    seen_chunk_ids: set[UUID] = set()
    context_text = "[]"

    for hit in hits:
        if len(sources) >= MAX_CONTEXT_SOURCES:
            break

        if hit.chunk_id in seen_chunk_ids:
            continue

        content = hit.content.strip()[:MAX_SOURCE_CHARS]

        if not content:
            continue

        source = AnswerSource(
            source_id=f"S{len(sources) + 1}",
            chunk_id=hit.chunk_id,
            document_id=hit.document_id,
            chunk_index=hit.chunk_index,
            content=content,
        )

        candidate_text = json.dumps(
            [
                {
                    "source_id": item.source_id,
                    "content": item.content,
                }
                for item in (*sources, source)
            ],
            ensure_ascii=False,
        )

        if len(candidate_text) > MAX_CONTEXT_CHARS:
            continue

        sources.append(source)
        seen_chunk_ids.add(hit.chunk_id)
        context_text = candidate_text

    return PreparedAnswerContext(
        text=context_text,
        sources=tuple(sources),
    )
