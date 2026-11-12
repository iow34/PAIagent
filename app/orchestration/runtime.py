from dataclasses import dataclass

from app.orchestration.budget import QueryExecutionBudget
from app.security.context import AccessContext
from app.services.generation_service import GenerationService
from app.services.search_service import SearchService


@dataclass(frozen=True)
class QueryRuntimeContext:
    access_context: AccessContext
    search_service: SearchService
    generation_service: GenerationService
    budget: QueryExecutionBudget
