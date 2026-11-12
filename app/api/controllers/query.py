import logging
from uuid import UUID

import msgspec
from litestar import Controller, post
from litestar.di import NamedDependency, Provide
from litestar.exceptions import HTTPException
from litestar.middleware import DefineMiddleware
from litestar.params import JSONBody
from openai import APIError, APITimeoutError
from sqlalchemy.exc import SQLAlchemyError

from app.api.execution_timeout import ExecutionTimeoutMiddleware
from app.query_types import QueryExecutionError, QueryResult
from app.repositories.conversations import (
    ConversationNotFoundError,
    ConversationRequestConflictError,
)
from app.security.authentication import get_access_context
from app.security.context import AccessContext
from app.services.embedding_service import EmbeddingResponseError
from app.services.generation_service import GenerationResponseError
from app.services.query_service import QueryInputError, QueryService

logger = logging.getLogger(__name__)


class QueryRequest(msgspec.Struct, forbid_unknown_fields=True):
    message: str
    session_id: UUID | None = None


class QueryController(Controller):
    path = "/v1/query"
    middleware = [
        DefineMiddleware(ExecutionTimeoutMiddleware, error_code="query_timeout")
    ]

    security = [{"ServiceAPIKey": []}]

    dependencies = {
        "access_context": Provide(get_access_context),
    }

    @post(status_code=200, request_max_body_size=16 * 1024)
    async def query(
        self,
        data: JSONBody[QueryRequest],
        access_context: NamedDependency[AccessContext],
        query_service: NamedDependency[QueryService],
    ) -> QueryResult:
        try:
            return await query_service.query(
                context=access_context,
                question=data.message,
                session_id=data.session_id,
            )
        except QueryInputError as error:
            raise HTTPException(
                status_code=400,
                detail=str(error),
                extra={"code": "invalid_query_input"},
            ) from error
        except ConversationNotFoundError as error:
            raise HTTPException(
                status_code=404,
                detail="Conversation is unavailable.",
                extra={"code": "conversation_not_found"},
            ) from error
        except ConversationRequestConflictError as error:
            raise HTTPException(
                status_code=409,
                detail="Conversation changed or the request was already saved. Retry with the latest history.",
                extra={"code": "conversation_request_conflict"},
            ) from error
        except (TimeoutError, APITimeoutError) as error:
            raise HTTPException(
                status_code=504,
                detail="Query execution timed out.",
                extra={"code": "query_timeout"},
            ) from error
        except (
            EmbeddingResponseError,
            GenerationResponseError,
        ) as error:
            raise HTTPException(
                status_code=502,
                detail="The model provider returned an invalid response.",
                extra={"code": "invalid_model_response"},
            ) from error
        except APIError as error:
            raise HTTPException(
                status_code=503,
                detail="The model provider is unavailable.",
                extra={"code": "model_provider_error"},
            ) from error
        except QueryExecutionError as error:
            logger.error(
                "Query workflow failed (%s).",
                type(error).__name__,
            )
            raise HTTPException(
                status_code=500,
                detail="Query workflow failed.",
                extra={"code": "query_execution_error"},
            ) from error
        except SQLAlchemyError as error:
            logger.error(
                "Query storage failed (%s).",
                type(error).__name__,
            )
            raise HTTPException(
                status_code=500,
                detail="Query storage failed.",
                extra={"code": "query_storage_error"},
            ) from error
