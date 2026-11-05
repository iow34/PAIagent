from uuid import UUID

import msgspec


class SearchHit(msgspec.Struct, frozen=True):
    chunk_id: UUID
    document_id: UUID
    chunk_index: int
    content: str
    score: float
