import argparse
import asyncio
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import get_settings
from app.db.models import ServiceAPIKey, Tenant
from app.db.postgres import open_database
from app.security.api_keys import generate_api_key, hash_api_key


async def issue_api_key(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    name: str,
) -> tuple[UUID, str]:
    """Create an API key and return its ID and secret value."""

    async with session_factory.begin() as session:
        tenant = await session.get(Tenant, tenant_id)

        if tenant is None:
            raise ValueError("A tenant with this identifier does not exist.")

        raw_key = generate_api_key()

        api_key = ServiceAPIKey(
            tenant_id=tenant.id,
            name=name,
            key_hash=hash_api_key(raw_key),
            is_active=True,
        )

        session.add(api_key)
        await session.flush()

        key_id = api_key.id

    return key_id, raw_key


async def run(*, tenant_id: UUID, name: str) -> None:
    """Create an API key and release database resources."""

    settings = get_settings()

    async with open_database(settings.database_url) as database:
        key_id, raw_key = await issue_api_key(
            session_factory=database.session_factory,
            tenant_id=tenant_id,
            name=name,
        )

    print("API key created.")
    print(f"Tenant ID: {tenant_id}")
    print(f"Key ID: {key_id}")
    print(f"Name: {name}")
    print()
    print("Keep the secret key safe: only its hash is stored in the database.")
    print(raw_key)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create an API key for an existing tenant."
    )
    parser.add_argument(
        "--tenant-id",
        type=UUID,
        required=True,
        help="UUID tenant, to whom the key will belong.",
    )
    parser.add_argument(
        "--name", required=True, help="A clear key name, for example, local-backend."
    )

    args = parser.parse_args()

    name = args.name.strip()

    if not 1 <= len(name) <= 255:
        parser.error("The key name must contain between 1 and 255 characters.")

    try:
        asyncio.run(
            run(
                tenant_id=args.tenant_id,
                name=name,
            )
        )
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
