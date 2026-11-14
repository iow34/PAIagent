import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()


async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])

    version = await conn.fetchval("SELECT version();")
    print("Connection to PostgreSQL successful.")
    print("Version:", version)

    has_vector = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector');"
    )
    print("pgvector on:", has_vector)

    await conn.close()


asyncio.run(main())
