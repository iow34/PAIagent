from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.orchestration.nodes import (
    generate_node,
    no_sources_node,
    prepare_context_node,
    prepare_query_node,
    retrieve_node,
    route_after_context,
    validate_node,
)
from app.orchestration.runtime import QueryRuntimeContext
from app.orchestration.state import QueryState


def build_query_graph() -> CompiledStateGraph[
    QueryState,
    QueryRuntimeContext,
]:
    builder = StateGraph[
        QueryState,
        QueryRuntimeContext,
    ](
        QueryState,
        context_schema=QueryRuntimeContext,
    )

    builder.add_node("prepare_query", prepare_query_node)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("prepare_context", prepare_context_node)
    builder.add_node("generate", generate_node)
    builder.add_node("validate", validate_node)
    builder.add_node("no_sources", no_sources_node)

    builder.add_edge(START, "prepare_query")
    builder.add_edge("prepare_query", "retrieve")
    builder.add_edge("retrieve", "prepare_context")

    builder.add_conditional_edges(
        "prepare_context",
        route_after_context,
        {
            "generate": "generate",
            "no_sources": "no_sources",
        },
    )

    builder.add_edge("generate", "validate")
    builder.add_edge("validate", END)
    builder.add_edge("no_sources", END)

    return builder.compile()
