import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()


async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])

    version = await conn.fetchval("SELECT version();")
    print("Підключення до PostgreSQL успішне.")
    print("Версія:", version)

    has_vector = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector');"
    )
    print("pgvector увімкнено:", has_vector)

    await conn.close()


asyncio.run(main())