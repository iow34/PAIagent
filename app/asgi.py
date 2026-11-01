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
from app.config.settings import get_settings
from app.db.models import EMBEDDING_DIM
from app.db.postgres import DatabaseResources, open_database
from app.services.embedding_service import EmbeddingService


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
        yield


def get_openai_client(state: State) -> AsyncOpenAI:
    return cast(AsyncOpenAI, state.openai_client)


def get_embedding_service(state: State) -> EmbeddingService:
    return cast(EmbeddingService, state.embedding_service)


def create_app() -> Litestar:
    return Litestar(
        route_handlers=[HealthController, DocumentController],
        lifespan=[database_lifespan, openai_lifespan],
        dependencies={
            "settings": Provide(get_settings, sync_to_thread=False),
            "db_session_factory": Provide(get_db_session_factory),
            "openai_client": Provide(get_openai_client, sync_to_thread=False),
            "embedding_service": Provide(get_embedding_service, sync_to_thread=False),
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
