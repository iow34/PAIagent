from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@dataclass(frozen=True)
class DatabaseResources:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]


def _to_asyncpg_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


@asynccontextmanager
async def open_database(
    database_url: str,
) -> AsyncIterator[DatabaseResources]:
    engine = create_async_engine(
        _to_asyncpg_url(database_url),
        echo=False,
    )

    try:
        session_factory = async_sessionmaker(
            engine,
            expire_on_commit=False,
        )

        yield DatabaseResources(
            engine=engine,
            session_factory=session_factory,
        )
    finally:
        await engine.dispose()
