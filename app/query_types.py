from typing import Literal, Protocol
from uuid import UUID

import msgspec

from app.answer_types import AnswerResult, AnswerSource
from app.security.context import AccessContext


class PreparedConversationHistory(msgspec.Struct, frozen=True):
    text: str
    message_count: int


class QueryResult(msgspec.Struct, frozen=True):
    session_id: UUID
    request_id: UUID
    answer: str
    sources: tuple[AnswerSource, ...]
    status: Literal["answered", "insufficient_information"]


class QueryExecutionError(RuntimeError):
    pass


class QueryWorkflow(Protocol):
    async def run(
        self,
        *,
        context: AccessContext,
        question: str,
        history: PreparedConversationHistory,
    ) -> AnswerResult: ...
