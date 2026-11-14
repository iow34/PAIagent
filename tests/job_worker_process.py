"""Test-only real worker subprocess against the runner's isolated PostgreSQL."""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_postgres_capabilities import FakeGenerator
from test_postgres_indexing import FakeEmbeddings

from app.jobs.worker import serve


async def main():
    url = make_url(os.environ["PAIAGENT_TEST_DATABASE_URL"])
    schema = os.environ["PAIAGENT_TEST_SCHEMA"]
    if not (url.database or "").endswith("_test") or not schema.startswith(
        "paiagent_test_"
    ):
        raise ValueError("Only an isolated test database/schema is allowed.")
    engine = create_async_engine(
        url, execution_options={"schema_translate_map": {None: schema}}
    )

    async def block():
        await asyncio.sleep(300)

    try:
        await serve(
            async_sessionmaker(engine, expire_on_commit=False),
            FakeGenerator(callback=block if sys.argv[1] == "block" else None),
            FakeEmbeddings(),
            poll_seconds=0.1,
            once=sys.argv[1] == "drain",
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
