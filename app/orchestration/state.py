from typing import NotRequired, TypedDict

from app.answer_types import AnswerResult
from app.query_types import PreparedConversationHistory
from app.search_types import SearchHit
from app.services.answer_context import PreparedAnswerContext
from app.services.generation_service import GeneratedAnswer


class QueryState(TypedDict):
    question: str
    history: PreparedConversationHistory
    search_query: NotRequired[str]
    hits: NotRequired[list[SearchHit]]
    prepared_context: NotRequired[PreparedAnswerContext]
    generated_answer: NotRequired[GeneratedAnswer]
    result: NotRequired[AnswerResult]
