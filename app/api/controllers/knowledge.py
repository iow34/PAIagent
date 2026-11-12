import logging
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from litestar import Controller, get, post
from litestar.di import NamedDependency, Provide
from litestar.exceptions import HTTPException
from litestar.middleware import DefineMiddleware
from openai import APIError, APITimeoutError
from sqlalchemy.exc import SQLAlchemyError

from app.api.execution_timeout import ExecutionTimeoutMiddleware
from app.knowledge_types import DocumentKnowledge
from app.orchestration.budget import QueryBudgetExceededError
from app.orchestration.knowledge_workflow import KnowledgeWorkflow
from app.repositories.knowledge import (
    KnowledgeDocumentNotFoundError,
    KnowledgeDocumentNotReadyError,
    KnowledgeNotFoundError,
    KnowledgeSourceConflictError,
)
from app.security.authentication import get_access_context
from app.security.context import AccessContext
from app.services.knowledge_context import KnowledgeContextError
from app.services.knowledge_generation import KnowledgeResponseError
from app.services.knowledge_service import KnowledgeStorageError

logger = logging.getLogger(__name__)


@contextmanager
def _knowledge_errors() -> Iterator[None]:
    try:
        yield
    except (KnowledgeDocumentNotFoundError, KnowledgeNotFoundError) as error:
        raise HTTPException(
            status_code=404,
            detail="Document knowledge is unavailable.",
            extra={"code": "knowledge_not_found"},
        ) from error
    except (KnowledgeDocumentNotReadyError, KnowledgeSourceConflictError) as error:
        raise HTTPException(
            status_code=409,
            detail="Document is not ready or changed during analysis.",
            extra={"code": "knowledge_document_conflict"},
        ) from error
    except KnowledgeContextError as error:
        raise HTTPException(
            status_code=409,
            detail="Document fragments cannot be analyzed.",
            extra={"code": "invalid_knowledge_context"},
        ) from error
    except (TimeoutError, APITimeoutError) as error:
        raise HTTPException(
            status_code=504,
            detail="Knowledge execution timed out.",
            extra={"code": "knowledge_timeout"},
        ) from error
    except KnowledgeResponseError as error:
        raise HTTPException(
            status_code=502,
            detail="The model provider returned an invalid knowledge response.",
            extra={"code": "invalid_knowledge_response"},
        ) from error
    except APIError as error:
        raise HTTPException(
            status_code=503,
            detail="The model provider is unavailable.",
            extra={"code": "model_provider_error"},
        ) from error
    except QueryBudgetExceededError as error:
        raise HTTPException(
            status_code=500,
            detail="Knowledge execution exceeded its call budget.",
            extra={"code": "knowledge_budget_exceeded"},
        ) from error
    except (SQLAlchemyError, KnowledgeStorageError) as error:
        logger.error("Knowledge storage failed (%s).", type(error).__name__)
        raise HTTPException(
            status_code=500,
            detail="Knowledge storage failed.",
            extra={"code": "knowledge_storage_error"},
        ) from error


class KnowledgeController(Controller):
    path = "/v1/documents/{document_id:uuid}/knowledge"
    security = [{"ServiceAPIKey": []}]
    dependencies = {"access_context": Provide(get_access_context)}
    middleware = [
        DefineMiddleware(ExecutionTimeoutMiddleware, error_code="knowledge_timeout")
    ]

    @post(status_code=200)
    async def analyze(
        self,
        document_id: UUID,
        access_context: NamedDependency[AccessContext],
        knowledge_workflow: NamedDependency[KnowledgeWorkflow],
    ) -> DocumentKnowledge:
        with _knowledge_errors():
            return await knowledge_workflow.run(
                context=access_context, document_id=document_id
            )

    @get()
    async def get_saved(
        self,
        document_id: UUID,
        access_context: NamedDependency[AccessContext],
        knowledge_workflow: NamedDependency[KnowledgeWorkflow],
    ) -> DocumentKnowledge:
        with _knowledge_errors():
            return await knowledge_workflow.get_saved(
                context=access_context, document_id=document_id
            )
