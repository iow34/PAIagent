from typing import Literal
from uuid import UUID

import msgspec


class KnowledgeSource(msgspec.Struct, frozen=True):
    chunk_id: UUID
    chunk_index: int
    content: str


class KnowledgeFact(msgspec.Struct, frozen=True):
    text: str
    chunk_ids: tuple[UUID, ...]


class DocumentKnowledge(msgspec.Struct, frozen=True):
    document_id: UUID
    summary: str
    summary_chunk_ids: tuple[UUID, ...]
    topics: tuple[str, ...]
    facts: tuple[KnowledgeFact, ...]
    sources: tuple[KnowledgeSource, ...]
    coverage: Literal["full", "partial"]
