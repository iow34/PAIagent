"""Administrative tenant provisioning; no public unauthenticated signup endpoint."""

import argparse
import asyncio
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import get_settings
from app.db.models import Tenant
from app.db.postgres import open_database


async def create_tenant(
    session_factory: async_sessionmaker[AsyncSession], name: str
) -> UUID:
    name = name.strip()
    if not 1 <= len(name) <= 255:
        raise ValueError("The tenant name must contain between 1 and 255 characters.")
    async with session_factory.begin() as session:
        tenant = Tenant(name=name)
        session.add(tenant)
        await session.flush()
        identifier = tenant.id
    return identifier


async def run(name: str) -> None:
    async with open_database(get_settings().database_url) as database:
        print(await create_tenant(database.session_factory, name))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a tenant; prints its UUID.")
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.name))
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
