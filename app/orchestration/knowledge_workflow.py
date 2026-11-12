from uuid import UUID

from app.knowledge_types import DocumentKnowledge
from app.orchestration.budget import QueryExecutionBudget
from app.security.context import AccessContext
from app.services.knowledge_service import KnowledgeService


class KnowledgeWorkflow:
    def __init__(
        self,
        *,
        knowledge_service: KnowledgeService,
    ) -> None:
        self._knowledge_service = knowledge_service

    async def get_saved(
        self, *, context: AccessContext, document_id: UUID
    ) -> DocumentKnowledge:
        return await self._knowledge_service.get_saved(
            context=context, document_id=document_id
        )

    async def run(
        self,
        *,
        context: AccessContext,
        document_id: UUID,
        budget: QueryExecutionBudget | None = None,
    ) -> DocumentKnowledge:
        if budget is None:
            budget = QueryExecutionBudget()

        budget.reserve(
            specialist_calls=1,
            tool_calls=2,
            model_calls=1,
        )

        return await self._knowledge_service.analyze(
            context=context,
            document_id=document_id,
        )
