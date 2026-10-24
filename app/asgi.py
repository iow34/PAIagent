from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from litestar import Litestar
from litestar.datastructures import State
from litestar.di import Provide
from litestar.openapi import OpenAPIConfig
from litestar.openapi.spec import Components, SecurityScheme
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.controllers.documents import DocumentController
from app.api.controllers.health import HealthController
from app.config.settings import get_settings
from app.db.postgres import DatabaseResources, open_database


@asynccontextmanager
async def database_lifespan(
    app: Litestar,
) -> AsyncIterator[None]:
    settings = get_settings()

    async with open_database(settings.database_url) as database:
        app.state.database = database
        yield


async def get_db_session_factory(
    state: State,
) -> async_sessionmaker[AsyncSession]:
    database = cast(DatabaseResources, state.database)
    return database.session_factory


def create_app() -> Litestar:
    return Litestar(
        route_handlers=[HealthController, DocumentController],
        lifespan=[database_lifespan],
        dependencies={
            "settings": Provide(get_settings, sync_to_thread=False),
            "db_session_factory": Provide(get_db_session_factory, sync_to_thread=False),
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
