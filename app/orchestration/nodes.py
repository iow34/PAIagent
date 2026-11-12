from typing import Literal

from langgraph.runtime import Runtime

from app.answer_types import AnswerResult
from app.orchestration.runtime import QueryRuntimeContext
from app.orchestration.state import QueryState
from app.query_types import QueryExecutionError
from app.search_types import SearchHit
from app.services.answer_context import (
    MAX_CONTEXT_SOURCES,
    PreparedAnswerContext,
    prepare_answer_context,
)
from app.services.answer_validation import validate_answer
from app.services.generation_service import GeneratedAnswer


async def prepare_query_node(
    state: QueryState,
    runtime: Runtime[QueryRuntimeContext],
) -> dict[str, str]:
    if state["history"].message_count > 0:
        runtime.context.budget.reserve(model_calls=1)

    search_query = await runtime.context.generation_service.prepare_search_query(
        question=state["question"],
        history=state["history"],
    )

    return {"search_query": search_query}


async def retrieve_node(
    state: QueryState,
    runtime: Runtime[QueryRuntimeContext],
) -> dict[str, list[SearchHit]]:
    if "search_query" not in state:
        raise QueryExecutionError("Retrieval requires a prepared search query.")

    runtime.context.budget.reserve(
        specialist_calls=1,
        tool_calls=1,
        model_calls=1,
    )

    hits = await runtime.context.search_service.search(
        context=runtime.context.access_context,
        query=state["search_query"],
        limit=MAX_CONTEXT_SOURCES,
    )

    return {"hits": hits}


def prepare_context_node(
    state: QueryState,
) -> dict[str, PreparedAnswerContext]:
    if "hits" not in state:
        raise QueryExecutionError("Context preparation requires search results.")

    prepared_context = prepare_answer_context(state["hits"])

    return {"prepared_context": prepared_context}


def route_after_context(
    state: QueryState,
) -> Literal["generate", "no_sources"]:
    if "prepared_context" not in state:
        raise QueryExecutionError("Routing requires prepared context.")

    if state["prepared_context"].sources:
        return "generate"

    return "no_sources"


async def generate_node(
    state: QueryState,
    runtime: Runtime[QueryRuntimeContext],
) -> dict[str, GeneratedAnswer]:
    if "prepared_context" not in state:
        raise QueryExecutionError("Generation requires prepared context.")

    if state["prepared_context"].sources:
        runtime.context.budget.reserve(model_calls=1)

    generated = await runtime.context.generation_service.generate(
        question=state["question"],
        history=state["history"],
        context=state["prepared_context"],
    )

    return {"generated_answer": generated}


def validate_node(
    state: QueryState,
) -> dict[str, AnswerResult]:
    if "generated_answer" not in state:
        raise QueryExecutionError("Answer validation requires a generated answer.")

    if "prepared_context" not in state:
        raise QueryExecutionError("Answer validation requires prepared context.")

    result = validate_answer(
        generated=state["generated_answer"],
        context=state["prepared_context"],
    )

    return {"result": result}


def no_sources_node(
    state: QueryState,
) -> dict[str, AnswerResult]:
    result = AnswerResult(
        answer="There is insufficient information in the available documents.",
        sources=(),
        status="insufficient_information",
    )

    return {"result": result}
