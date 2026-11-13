"""One fixed LangGraph workflow; profiles select adapters, never executable nodes."""

import json
import re
from typing import Any, Protocol, TypedDict

import msgspec
from langgraph.graph import END, START, StateGraph

from app.capabilities.generation import Meter, response_schema
from app.integration.validation import ContractError


class State(TypedDict):
    config: Any
    run: Any
    sources: list[dict[str, Any]]
    metrics: list[dict[str, Any]]
    meter: Meter
    partial: bool
    payload: dict[str, Any]
    data_state: str
    processed_ids: list[str]


def batches(sources, maximum):
    batch, size = [], 0
    for source in sources:
        # Split long selected field text too. ID still refers to the full evidence.
        content = source["content"]
        step = max(100, maximum - 1500)
        for offset in range(0, len(content), step):
            part = {**source, "content": content[offset : offset + step]}
            length = len(json.dumps(part, ensure_ascii=False))
            if length > maximum:
                raise ContractError(
                    "Source metadata exceeds the context budget.", code="context_limit"
                )
            if batch and size + length > maximum:
                yield batch
                batch, size = [], 0
            batch.append(part)
            size += length
    if batch:
        yield batch


def request_for(state, sources, capability=None):
    config, run = state["config"], state["run"]
    return {
        "capability": capability or config.capability,
        "goal": config.goal,
        "language": config.language,
        "constraints": config.constraints,
        "question": run.query,
        "history": [{"role": h.role, "content": h.content} for h in run.history],
        "period": msgspec.to_builtins(config.period),
        "data_coverage": "partial" if state["partial"] else "selected_input_only",
        "sources": sources,
        # Numeric data is returned by the server; prose only receives a bounded
        # advisory description. No model-produced numeric payload is trusted.
        "exact_metrics": [
            {k: v for k, v in m.items() if k != "source_ids"} for m in state["metrics"]
        ][:100],
    }


class Adapter(Protocol):
    async def execute(self, state: State) -> dict[str, Any]: ...


class SearchAdapter:
    async def execute(self, state):
        items = state["sources"][: state["run"].limit]
        return {
            "payload": {"items": items},
            "data_state": "partial" if items else "insufficient_data",
            "processed_ids": [s["id"] for s in items],
            "partial": True,
        }


class GroundedAdapter:
    async def execute(self, state):
        config = state["config"]
        groups = list(batches(state["sources"], config.budget.context_chars))
        selected = groups[0] if groups else []
        partial = state["partial"] or len(groups) > 1
        generated = await state["meter"].generate(
            request_for({**state, "partial": partial}, selected),
            response_schema(config.capability, config.output_schema),
            {s["id"] for s in selected},
        )
        payload = {
            "claims": generated["claims"],
            "limitations": generated["limitations"],
        }
        if config.capability == "extract":
            payload["data"] = (
                generated["data"] if not generated["insufficient_data"] else None
            )
            payload["source_ids"] = (
                generated["source_ids"] if not generated["insufficient_data"] else []
            )
        if config.capability == "analyze":
            payload["metrics"] = state["metrics"]
        return {
            "payload": payload,
            "partial": partial,
            "data_state": "insufficient_data"
            if generated["insufficient_data"]
            else "partial"
            if partial
            else "sufficient",
            "processed_ids": list({s["id"] for s in selected}),
        }


class QueryAdapter(GroundedAdapter):
    """Current evidence answers; explicitly supplied history is context only."""


class ExtractAdapter(GroundedAdapter):
    """Strict schema extraction/classification; no dynamic tools."""


class AnalyzeAdapter(GroundedAdapter):
    """Server metrics plus grounded observations."""


class RecommendAdapter(GroundedAdapter):
    """Advisory suggestions; memory integration is reserved for step 11."""


class SummaryAdapter:
    async def execute(self, state):
        maximum = state["config"].budget.context_chars
        groups = list(batches(state["sources"], maximum))
        if len(groups) <= 1:
            return await GroundedAdapter().execute(state)
        original_ids = {s["id"] for s in state["sources"]}
        processed = set()
        summaries = []
        partial = state["partial"]
        schema = response_schema("summarize")
        for group in groups:
            allowed = {s["id"] for s in group}
            generated = await state["meter"].generate(
                request_for(state, group, "summarize"), schema, allowed
            )
            processed.update(allowed)
            partial |= generated["insufficient_data"]
            for claim in generated["claims"]:
                summaries.append(
                    {
                        "id": claim["source_ids"][0],
                        "content": claim["text"],
                        "support_ids": claim["source_ids"],
                    }
                )
        # Reduce until a bounded final synthesis fits. Budgets, not a graph loop,
        # bound the number of levels even if a provider fails to compress.
        while len(list(batches(summaries, maximum))) > 1:
            reduced = []
            for group in batches(summaries, maximum):
                allowed = {
                    sid for s in group for sid in s.get("support_ids", [s["id"]])
                }
                generated = await state["meter"].generate(
                    request_for(state, group, "summarize"), schema, allowed
                )
                partial |= generated["insufficient_data"]
                for claim in generated["claims"]:
                    reduced.append(
                        {
                            "id": claim["source_ids"][0],
                            "content": claim["text"],
                            "support_ids": claim["source_ids"],
                        }
                    )
            summaries = reduced
        if not summaries:
            return {
                "payload": {
                    "claims": [],
                    "limitations": ["No supported summary from the supplied parts."],
                },
                "data_state": "insufficient_data",
                "processed_ids": list(processed),
                "partial": partial,
            }
        allowed = {sid for s in summaries for sid in s["support_ids"]} & original_ids
        generated = await state["meter"].generate(
            request_for({**state, "partial": partial}, summaries, "summarize"),
            schema,
            allowed,
        )
        return {
            "payload": {
                "claims": generated["claims"],
                "limitations": generated["limitations"],
            },
            "data_state": "insufficient_data"
            if generated["insufficient_data"]
            else "partial"
            if partial
            else "sufficient",
            "processed_ids": list(processed),
            "partial": partial,
        }


ADAPTERS: dict[str, Adapter] = {
    "search": SearchAdapter(),
    "query": QueryAdapter(),
    "summarize": SummaryAdapter(),
    "extract": ExtractAdapter(),
    "analyze": AnalyzeAdapter(),
    "recommend": RecommendAdapter(),
}


def lexical_rank(sources, query):
    words = set(re.findall(r"\w+", query.casefold()))
    scored = []
    for source in sources:
        tokens = set(re.findall(r"\w+", source["content"].casefold()))
        score = len(words & tokens)
        if score:
            scored.append((score, source))
    return [s for _, s in sorted(scored, key=lambda pair: (-pair[0], pair[1]["id"]))]


class CapabilityWorkflow:
    def __init__(self):
        graph = StateGraph(State)
        graph.add_node("execute", self.execute)
        graph.add_edge(START, "execute")
        graph.add_edge("execute", END)
        self.graph = graph.compile()

    async def execute(self, state):
        if state["config"].capability != "search" and not state["sources"]:
            payload: dict[str, Any] = {
                "claims": [],
                "limitations": ["No matching evidence."],
            }
            if state["config"].capability == "analyze":
                payload["metrics"] = state["metrics"]
            if state["config"].capability == "extract":
                payload.update(data=None, source_ids=[])
            return {
                "payload": payload,
                "data_state": "insufficient_data",
                "processed_ids": [],
            }
        return await ADAPTERS[state["config"].capability].execute(state)

    async def run(self, state):
        return await self.graph.ainvoke(state, config={"recursion_limit": 5})
