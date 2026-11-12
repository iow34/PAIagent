import json
from collections.abc import Sequence
from typing import Literal
from uuid import UUID

import msgspec

from app.db.models import DocumentChunk
from app.knowledge_types import KnowledgeSource

MAX_KNOWLEDGE_SOURCES = 12
MAX_KNOWLEDGE_CONTEXT_CHARS = 24000


class KnowledgeContextError(ValueError):
    pass


class PreparedKnowledgeContext(msgspec.Struct, frozen=True):
    document_id: UUID
    text: str
    sources: tuple[KnowledgeSource, ...]
    coverage: Literal["full", "partial"]


def prepare_knowledge_context(
    *,
    document_id: UUID,
    chunks: Sequence[DocumentChunk],
) -> PreparedKnowledgeContext:
    if not chunks:
        raise KnowledgeContextError(
            "Knowledge analysis requires document chunks."
        )

    seen_chunk_ids: set[UUID] = set()

    for expected_index, chunk in enumerate(chunks):
        if chunk.document_id != document_id:
            raise KnowledgeContextError(
                "A chunk belongs to another document."
            )

        if chunk.chunk_index != expected_index:
            raise KnowledgeContextError(
                "Document chunks must have consecutive indices starting at zero."
            )

        if chunk.id in seen_chunk_ids:
            raise KnowledgeContextError(
                "Document chunks contain duplicate IDs."
            )

        if not chunk.content.strip():
            raise KnowledgeContextError(
                "A document chunk is empty."
            )

        seen_chunk_ids.add(chunk.id)

    sources: list[KnowledgeSource] = []
    context_text = "[]"

    for chunk in chunks:
        if len(sources) >= MAX_KNOWLEDGE_SOURCES:
            break

        source = KnowledgeSource(
            chunk_id=chunk.id,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
        )

        candidate_text = json.dumps(
            [
                {
                    "source_id": f"K{index}",
                    "chunk_index": item.chunk_index,
                    "content": item.content,
                }
                for index, item in enumerate(
                    (*sources, source),
                    start=1,
                )
            ],
            ensure_ascii=False,
        )

        if len(candidate_text) > MAX_KNOWLEDGE_CONTEXT_CHARS:
            break

        sources.append(source)
        context_text = candidate_text

    if not sources:
        raise KnowledgeContextError(
            "The first document chunk exceeds the knowledge context limit."
        )

    coverage: Literal["full", "partial"] = (
        "full" if len(sources) == len(chunks) else "partial"
    )

    return PreparedKnowledgeContext(
        document_id=document_id,
        text=context_text,
        sources=tuple(sources),
        coverage=coverage,
    )