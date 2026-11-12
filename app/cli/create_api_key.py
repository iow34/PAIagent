import argparse
import asyncio
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import get_settings
from app.db.models import IntegrationSource, ServiceAPIKey, Tenant
from app.db.postgres import open_database
from app.security.api_keys import generate_api_key, hash_api_key
from app.security.permissions import LEGACY_SCOPES, SCOPES


async def issue_api_key(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    name: str,
    scopes: tuple[str, ...] = LEGACY_SCOPES,
    source_ids: tuple[UUID, ...] | None = None,
) -> tuple[UUID, str]:
    """Create an API key and return its ID and secret value."""

    if not scopes or set(scopes) - SCOPES:
        raise ValueError("Specify at least one supported scope.")
    if source_ids is not None and "legacy:*" in scopes:
        raise ValueError("Legacy routes cannot be granted to source-restricted keys.")
    async with session_factory.begin() as session:
        tenant = await session.get(Tenant, tenant_id)

        if tenant is None:
            raise ValueError("A tenant with this identifier does not exist.")

        if source_ids is not None:
            owned = set(
                await session.scalars(
                    select(IntegrationSource.id).where(
                        IntegrationSource.tenant_id == tenant_id,
                        IntegrationSource.id.in_(source_ids),
                    )
                )
            )
            if not source_ids or owned != set(source_ids):
                raise ValueError("Every source must exist in the selected tenant.")
        raw_key = generate_api_key()

        api_key = ServiceAPIKey(
            tenant_id=tenant.id,
            name=name,
            key_hash=hash_api_key(raw_key),
            is_active=True,
            scopes=list(dict.fromkeys(scopes)),
            source_ids=None
            if source_ids is None
            else [str(value) for value in dict.fromkeys(source_ids)],
        )

        session.add(api_key)
        await session.flush()

        key_id = api_key.id

    return key_id, raw_key


async def run(
    *,
    tenant_id: UUID,
    name: str,
    scopes: tuple[str, ...] = LEGACY_SCOPES,
    source_ids: tuple[UUID, ...] | None = None,
) -> None:
    """Create an API key and release database resources."""

    settings = get_settings()

    async with open_database(settings.database_url) as database:
        key_id, raw_key = await issue_api_key(
            session_factory=database.session_factory,
            tenant_id=tenant_id,
            name=name,
            scopes=scopes,
            source_ids=source_ids,
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

    parser.add_argument(
        "--scope",
        action="append",
        choices=sorted(SCOPES),
        help="Repeat for each grant. Defaults to legacy:* and catalog:read.",
    )
    parser.add_argument(
        "--source-id",
        action="append",
        type=UUID,
        help="Repeat to restrict the key to existing sources. Omission allows all tenant sources.",
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
                scopes=tuple(args.scope) if args.scope else LEGACY_SCOPES,
                source_ids=tuple(args.source_id) if args.source_id else None,
            )
        )
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
