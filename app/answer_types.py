from typing import Literal
from uuid import UUID

import msgspec


class AnswerSource(msgspec.Struct, frozen=True):
    source_id: str
    chunk_id: UUID
    document_id: UUID
    chunk_index: int
    content: str


class AnswerResult(msgspec.Struct, frozen=True):
    answer: str
    sources: tuple[AnswerSource, ...]
    status: Literal["answered", "insufficient_information"]
