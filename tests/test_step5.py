"""Step 5 pure validation, real LangGraph execution and provider error tests."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.db.models import ConversationMessage, DocumentChunk
from app.orchestration.budget import QueryBudgetExceededError, QueryExecutionBudget
from app.orchestration.query_workflow import LangGraphQueryWorkflow
from app.query_types import PreparedConversationHistory
from app.search_types import SearchHit
from app.security.context import AccessContext
from app.services.answer_context import prepare_answer_context
from app.services.answer_validation import validate_answer
from app.services.conversation_history import prepare_conversation_history
from app.services.generation_service import (
    GeneratedAnswer,
    GenerationResponseError,
    GenerationService,
)
from app.services.knowledge_context import (
    KnowledgeContextError,
    prepare_knowledge_context,
)
from app.services.knowledge_generation import (
    GeneratedDocumentKnowledge,
    GeneratedKnowledgeFact,
    KnowledgeGenerationService,
    KnowledgeResponseError,
)
from app.services.knowledge_validation import validate_knowledge


def hit(content="Повернення можливе протягом 14 днів."):
    return SearchHit(uuid4(), uuid4(), 0, content, 1.0)


class ValidationTests(unittest.TestCase):
    def test_answer_references_are_validated_and_ordered(self):
        context = prepare_answer_context([hit(), hit()])
        answer = GeneratedAnswer(
            answer="Друге [S2], перше [S1], знову [S2].",
            source_ids=["S1", "S2"],
            status="answered",
        )
        result = validate_answer(generated=answer, context=context)
        self.assertEqual([s.source_id for s in result.sources], ["S2", "S1"])
        for text, ids in (
            ("Текст [S99]", ["S99"]),
            ("Текст [S1, S2]", ["S1", "S2"]),
            ("Текст [S1]", ["S1", "S1"]),
            ("Текст\x00 [S1]", ["S1"]),
        ):
            with self.subTest(text=text), self.assertRaises(GenerationResponseError):
                validate_answer(
                    generated=GeneratedAnswer(
                        answer=text, source_ids=ids, status="answered"
                    ),
                    context=context,
                )

    def test_insufficient_information_cannot_cite_sources(self):
        with self.assertRaises(GenerationResponseError):
            validate_answer(
                generated=GeneratedAnswer(
                    answer="Немає [S1]",
                    source_ids=["S1"],
                    status="insufficient_information",
                ),
                context=prepare_answer_context([hit()]),
            )

    def test_history_keeps_pairs_and_removes_old_markers(self):
        session_id = uuid4()
        messages = []
        for i in range(6):
            request_id = uuid4()
            for offset, role in enumerate(("user", "assistant")):
                messages.append(
                    ConversationMessage(
                        session_id=session_id,
                        request_id=request_id,
                        position=i * 2 + offset,
                        role=role,
                        content=f"Повідомлення {i} [S1]",
                    )
                )
        history = prepare_conversation_history(messages)
        self.assertEqual(history.message_count, 10)
        self.assertEqual(json.loads(history.text)[0]["content"], "Повідомлення 1")
        self.assertNotIn("[S1]", history.text)
        self.assertIn("[S1]", messages[0].content)

    def test_knowledge_context_coverage_and_mapping(self):
        document_id = uuid4()
        chunks = [
            DocumentChunk(
                id=uuid4(), document_id=document_id, chunk_index=i, content="Текст"
            )
            for i in range(13)
        ]
        context = prepare_knowledge_context(document_id=document_id, chunks=chunks)
        self.assertEqual(context.coverage, "partial")
        self.assertEqual(len(context.sources), 12)
        self.assertEqual(json.loads(context.text)[-1]["source_id"], "K12")
        generated = GeneratedDocumentKnowledge(
            summary="Опис",
            summary_source_ids=["K2"],
            topics=["Тема"],
            facts=[GeneratedKnowledgeFact(text="Факт", source_ids=["K1"])],
        )
        result = validate_knowledge(generated=generated, context=context)
        self.assertEqual(result.summary_chunk_ids, (chunks[1].id,))
        self.assertEqual(result.facts[0].chunk_ids, (chunks[0].id,))
        self.assertEqual(result.coverage, "partial")
        self.assertEqual(
            prepare_knowledge_context(
                document_id=document_id, chunks=chunks[:2]
            ).coverage,
            "full",
        )

    def test_knowledge_rejects_invalid_context_and_output(self):
        document_id = uuid4()
        chunk = DocumentChunk(
            id=uuid4(), document_id=document_id, chunk_index=0, content="Текст"
        )
        with self.assertRaises(KnowledgeContextError):
            prepare_knowledge_context(document_id=uuid4(), chunks=[chunk])
        context = prepare_knowledge_context(document_id=document_id, chunks=[chunk])
        valid = dict(summary="Опис", summary_source_ids=["K1"], topics=[], facts=[])
        for changes in (
            {"summary_source_ids": ["K99"]},
            {"summary_source_ids": []},
            {"summary": " "},
            {"topics": ["Тема", "тема"]},
            {"summary": "x" * 1501},
            {"summary": "Опис\x00"},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(KnowledgeResponseError),
            ):
                validate_knowledge(
                    generated=GeneratedDocumentKnowledge(**(valid | changes)),
                    context=context,
                )

    def test_budgets_are_independent_and_reservations_atomic(self):
        budget = QueryExecutionBudget()
        budget.reserve(model_calls=8)
        with self.assertRaises(QueryBudgetExceededError):
            budget.reserve(model_calls=1, tool_calls=2)
        self.assertEqual(budget.tool_calls, 0)
        self.assertEqual(QueryExecutionBudget().model_calls, 0)
        with self.assertRaises(ValueError):
            budget.reserve(tool_calls=-1)


class GraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_graph_rewrites_search_and_passes_history(self):
        context = AccessContext(uuid4(), "user")
        history = PreparedConversationHistory(
            '[{"role":"user","content":"Повернення"}]', 1
        )
        search = SimpleNamespace(search=AsyncMock(return_value=[hit()]))
        generation = SimpleNamespace(
            prepare_search_query=AsyncMock(return_value="Винятки повернення"),
            generate=AsyncMock(
                return_value=GeneratedAnswer(
                    answer="Правило [S1].", source_ids=["S1"], status="answered"
                )
            ),
        )
        workflow = LangGraphQueryWorkflow(search, generation)
        result = await workflow.run(
            context=context, question="Які винятки?", history=history
        )
        self.assertEqual(result.status, "answered")
        self.assertEqual(search.search.call_args.kwargs["query"], "Винятки повернення")
        self.assertEqual(search.search.call_args.kwargs["context"], context)
        self.assertEqual(generation.generate.call_args.kwargs["history"], history)

    async def test_no_sources_skip_generation(self):
        generation = SimpleNamespace(
            prepare_search_query=AsyncMock(return_value="Запит"), generate=AsyncMock()
        )
        workflow = LangGraphQueryWorkflow(
            SimpleNamespace(search=AsyncMock(return_value=[])), generation
        )
        result = await workflow.run(
            context=AccessContext(uuid4(), "u"),
            question="Запит",
            history=PreparedConversationHistory("[]", 0),
        )
        self.assertEqual(result.status, "insufficient_information")
        generation.generate.assert_not_awaited()

    async def test_generation_services_reject_incomplete_and_missing_results(self):
        doc_id = uuid4()
        knowledge_context = prepare_knowledge_context(
            document_id=doc_id,
            chunks=[
                DocumentChunk(
                    id=uuid4(), document_id=doc_id, chunk_index=0, content="Текст"
                )
            ],
        )
        parse = AsyncMock()
        client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
        service = KnowledgeGenerationService(client, model="test")
        for status in ("incomplete", "completed"):
            parse.return_value = SimpleNamespace(status=status, output_parsed=None)
            with self.assertRaises(KnowledgeResponseError):
                await service.generate(context=knowledge_context)

        generator = GenerationService(client, model="test")
        parse.reset_mock()
        self.assertEqual(
            await generator.prepare_search_query(
                question="Питання", history=PreparedConversationHistory("[]", 0)
            ),
            "Питання",
        )
        parse.assert_not_awaited()

        async def slow(**kwargs):
            await asyncio.sleep(1)

        parse.side_effect = slow
        with patch(
            "app.services.knowledge_generation.KNOWLEDGE_GENERATION_TIMEOUT_SECONDS",
            0.01,
        ):
            with self.assertRaises(TimeoutError):
                await service.generate(context=knowledge_context)


if __name__ == "__main__":
    unittest.main()
