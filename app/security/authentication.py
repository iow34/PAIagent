from typing import Annotated, Any

from litestar import Request
from litestar.di import NamedDependency
from litestar.exceptions import NotAuthorizedException, ValidationException
from litestar.params import Parameter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.api_keys import APIKeysRepository
from app.security.api_keys import hash_api_key
from app.security.context import AccessContext


async def get_access_context(
    request: Request[Any, Any, Any],
    user_id: Annotated[
        str,
        Parameter(
            header="X-User-ID",
            min_length=1,
            max_length=255,
            description="User ID from the trusted backend.",
        ),
    ],
    db_session_factory: NamedDependency[async_sessionmaker[AsyncSession]],
) -> AccessContext:
    authorization = request.headers.get("Authorization", "")
    parts = authorization.split()

    if len(parts) != 2 or parts[0].lower() != "bearer" or len(parts[1]) > 256:
        raise NotAuthorizedException(
            detail="Missing or invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    async with db_session_factory.begin() as session:
        repository = APIKeysRepository(session)

        tenant_id = await repository.get_active_tenant_id(
            key_hash=hash_api_key(parts[1]),
        )

    if tenant_id is None:
        raise NotAuthorizedException(
            detail="Missing or invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user_id != user_id.strip() or any(
        ord(char) < 32 or ord(char) == 127 for char in user_id
    ):
        raise ValidationException(
            detail=(
                "X-User-ID must contain 1 to 255 characters "
                "without surrounding whitespace or control characters."
            ),
        )

    return AccessContext(tenant_id=tenant_id, user_id=user_id)
