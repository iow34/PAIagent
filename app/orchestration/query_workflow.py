from langgraph.errors import GraphRecursionError

from app.answer_types import AnswerResult
from app.orchestration.budget import QueryExecutionBudget
from app.orchestration.graph import build_query_graph
from app.orchestration.runtime import QueryRuntimeContext
from app.orchestration.state import QueryState
from app.query_types import (
    PreparedConversationHistory,
    QueryExecutionError,
)
from app.security.context import AccessContext
from app.services.generation_service import GenerationService
from app.services.search_service import SearchService

GRAPH_RECURSION_LIMIT = 10


class LangGraphQueryWorkflow:
    def __init__(
        self,
        search_service: SearchService,
        generation_service: GenerationService,
    ) -> None:
        self._search_service = search_service
        self._generation_service = generation_service
        self._graph = build_query_graph()

    async def run(
        self,
        *,
        context: AccessContext,
        question: str,
        history: PreparedConversationHistory,
    ) -> AnswerResult:
        initial_state: QueryState = {
            "question": question,
            "history": history,
        }

        runtime_context = QueryRuntimeContext(
            access_context=context,
            search_service=self._search_service,
            generation_service=self._generation_service,
            budget=QueryExecutionBudget(),
        )

        try:
            final_state = await self._graph.ainvoke(
                initial_state,
                config={
                    "recursion_limit": GRAPH_RECURSION_LIMIT,
                },
                context=runtime_context,
            )
        except GraphRecursionError as error:
            raise QueryExecutionError("Query exceeded the graph step limit.") from error

        result = final_state.get("result")

        if not isinstance(result, AnswerResult):
            raise QueryExecutionError("Query workflow did not return an AnswerResult.")

        return result
