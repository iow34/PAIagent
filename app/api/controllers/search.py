import logging
from typing import Literal
from uuid import UUID

import msgspec
from litestar import Controller, post
from litestar.di import NamedDependency, Provide
from litestar.exceptions import HTTPException
from litestar.params import JSONBody
from openai import APIError, APITimeoutError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.search_types import SearchHit
from app.security.authentication import get_access_context
from app.security.context import AccessContext
from app.services.embedding_service import (
    EmbeddingResponseError,
    EmbeddingService,
)
from app.services.search_service import SearchInputError, SearchService

logger = logging.getLogger(__name__)


class SearchRequest(msgspec.Struct, forbid_unknown_fields=True):
    query: str
    limit: int = 5
    document_id: UUID | None = None


class SearchResponse(msgspec.Struct):
    items: list[SearchHit]
    score_type: Literal["rrf"] = "rrf"


class SearchController(Controller):
    path = "/v1/search"

    security = [{"ServiceAPIKey": []}]

    dependencies = {
        "access_context": Provide(get_access_context),
    }

    @post(status_code=200, request_max_body_size=16 * 1024)
    async def search(
        self,
        data: JSONBody[SearchRequest],
        db_session_factory: NamedDependency[async_sessionmaker[AsyncSession]],
        access_context: NamedDependency[AccessContext],
        embedding_service: NamedDependency[EmbeddingService],
    ) -> SearchResponse:
        service = SearchService(db_session_factory, embedding_service)

        try:
            hits = await service.search(
                context=access_context,
                query=data.query,
                limit=data.limit,
                document_id=data.document_id,
            )
        except SearchInputError as error:
            raise HTTPException(
                status_code=400,
                detail=str(error),
                extra={"code": "invalid_search_input"},
            ) from error 
        except (TimeoutError, APITimeoutError) as error:
            raise HTTPException(
                status_code=504,
                detail="Search embedding request timed out.",
                extra={"code": "embedding_timeout"},
            ) from error
        except EmbeddingResponseError as error:
            raise HTTPException(
                status_code=502,
                detail="Embedding provider returned an invalid response.",
                extra={"code": "invalid_embedding_response"},
            ) from error 
        except APIError as error:
            raise HTTPException(
                status_code=503,
                detail="Embedding provider is unavailable.",
                extra={"code": "embedding_provider_error"},
            ) from error 
        except SQLAlchemyError as error:
            logger.error(
                "Search storage failed (%s).",
                type(error).__name__,
            )
            raise HTTPException(
                status_code=500,
                detail="Search storage failed.",
                extra={"code": "search_storage_error"},
            ) from error 

        return SearchResponse(items=hits)