from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from litestar import Litestar
from litestar.datastructures import State
from litestar.di import Provide
from litestar.openapi import OpenAPIConfig
from litestar.openapi.spec import Components, SecurityScheme
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.controllers.documents import DocumentController
from app.api.controllers.health import HealthController
from app.api.controllers.knowledge import KnowledgeController
from app.api.controllers.query import QueryController
from app.api.controllers.search import SearchController
from app.config.settings import get_settings
from app.db.models import EMBEDDING_DIM
from app.db.postgres import DatabaseResources, open_database
from app.orchestration.knowledge_workflow import KnowledgeWorkflow
from app.orchestration.query_workflow import LangGraphQueryWorkflow
from app.services.embedding_service import EmbeddingService
from app.services.generation_service import GenerationService
from app.services.knowledge_generation import KnowledgeGenerationService
from app.services.knowledge_service import KnowledgeService
from app.services.query_service import QueryService
from app.services.search_service import SearchService


@asynccontextmanager
async def database_lifespan(
    app: Litestar,
) -> AsyncIterator[None]:
    settings = get_settings()

    if settings.embedding_dimensions != EMBEDDING_DIM:
        raise ValueError("Embedding dimensions do not match the database model.")

    async with open_database(settings.database_url) as database:
        app.state.database = database
        yield


async def get_db_session_factory(
    state: State,
) -> async_sessionmaker[AsyncSession]:
    database = cast(DatabaseResources, state.database)
    return database.session_factory


@asynccontextmanager
async def openai_lifespan(
    app: Litestar,
) -> AsyncIterator[None]:
    settings = get_settings()

    async with AsyncOpenAI(
        api_key=settings.openai_api_key,
        timeout=settings.openai_timeout_seconds,
        max_retries=settings.openai_max_retries,
    ) as client:
        app.state.openai_client = client
        app.state.embedding_service = EmbeddingService(client, settings)
        app.state.generation_service = GenerationService(
            client,
            model=settings.generation_model,
        )
        app.state.knowledge_generation_service = KnowledgeGenerationService(
            client,
            model=settings.generation_model,
        )
        yield


@asynccontextmanager
async def query_lifespan(
    app: Litestar,
) -> AsyncIterator[None]:
    database = cast(
        DatabaseResources,
        app.state.database,
    )

    embedding_service = cast(
        EmbeddingService,
        app.state.embedding_service,
    )

    generation_service = cast(
        GenerationService,
        app.state.generation_service,
    )

    search_service = SearchService(
        database.session_factory,
        embedding_service,
    )

    workflow = LangGraphQueryWorkflow(
        search_service=search_service,
        generation_service=generation_service,
    )

    app.state.query_service = QueryService(
        session_factory=database.session_factory,
        workflow=workflow,
    )

    yield


@asynccontextmanager
async def knowledge_lifespan(app: Litestar) -> AsyncIterator[None]:
    database = cast(DatabaseResources, app.state.database)
    generation_service = cast(
        KnowledgeGenerationService, app.state.knowledge_generation_service
    )
    service = KnowledgeService(
        session_factory=database.session_factory,
        generation_service=generation_service,
    )
    app.state.knowledge_workflow = KnowledgeWorkflow(knowledge_service=service)
    yield


def get_knowledge_workflow(state: State) -> KnowledgeWorkflow:
    return cast(KnowledgeWorkflow, state.knowledge_workflow)


def get_openai_client(state: State) -> AsyncOpenAI:
    return cast(AsyncOpenAI, state.openai_client)


def get_embedding_service(state: State) -> EmbeddingService:
    return cast(EmbeddingService, state.embedding_service)


def get_query_service(state: State) -> QueryService:
    return cast(QueryService, state.query_service)


def create_app() -> Litestar:
    return Litestar(
        route_handlers=[
            HealthController,
            DocumentController,
            SearchController,
            QueryController,
            KnowledgeController,
        ],
        lifespan=[
            database_lifespan,
            openai_lifespan,
            query_lifespan,
            knowledge_lifespan,
        ],
        dependencies={
            "settings": Provide(get_settings, sync_to_thread=False),
            "db_session_factory": Provide(get_db_session_factory),
            "openai_client": Provide(get_openai_client, sync_to_thread=False),
            "embedding_service": Provide(get_embedding_service, sync_to_thread=False),
            "query_service": Provide(get_query_service, sync_to_thread=False),
            "knowledge_workflow": Provide(get_knowledge_workflow, sync_to_thread=False),
        },
        openapi_config=OpenAPIConfig(
            title="PAIagent API",
            version="0.1.0",
            components=Components(
                security_schemes={
                    "ServiceAPIKey": SecurityScheme(
                        type="http",
                        scheme="bearer",
                        description=(
                            "Enter the PAIagent service API key "
                            "without the 'Bearer ' prefix."
                        ),
                    ),
                },
            ),
        ),
    )
