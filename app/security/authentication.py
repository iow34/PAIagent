from typing import Annotated, Any
from uuid import UUID

from litestar import Request
from litestar.di import NamedDependency
from litestar.exceptions import (
    NotAuthorizedException,
    PermissionDeniedException,
    ValidationException,
)
from litestar.params import Parameter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.api_keys import APIKeysRepository
from app.security.api_keys import hash_api_key
from app.security.context import AccessContext
from app.security.permissions import require_scope


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

        api_key = await repository.get_active_key(
            key_hash=hash_api_key(parts[1]),
        )

    if api_key is None:
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

    context = AccessContext(
        api_key_id=api_key.id,
        tenant_id=api_key.tenant_id,
        user_id=user_id,
        scopes=tuple(api_key.scopes),
        source_ids=None
        if api_key.source_ids is None
        else tuple(UUID(value) for value in api_key.source_ids),
    )
    scope = request.route_handler.opt.get("required_scope", "legacy:*")
    require_scope(context, scope)
    if scope == "legacy:*" and context.source_ids is not None:
        raise PermissionDeniedException(
            detail="Legacy document routes do not support source-restricted keys.",
            extra={"code": "source_forbidden"},
        )
    return context
