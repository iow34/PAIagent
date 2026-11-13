"""Explicit key grants; a source restriction never grants an operation."""

from uuid import UUID

from litestar.exceptions import PermissionDeniedException

from app.security.context import AccessContext

LEGACY_SCOPES = ("legacy:*", "catalog:read")
SCOPES = frozenset(
    {
        *LEGACY_SCOPES,
        "sources:read",
        "sources:write",
        "collections:read",
        "collections:write",
        "schemas:read",
        "schemas:write",
        "data:validate",
        "data:read",
        "data:write",
        "data:delete",
        "profiles:read",
        "profiles:write",
        "ai:run",
        "runs:read",
        "runs:write",
        "results:read",
    }
)


def require_scope(context: AccessContext, scope: str) -> None:
    if scope not in context.scopes:
        raise PermissionDeniedException(
            detail="API key does not grant this operation.",
            extra={"code": "insufficient_scope"},
        )


def require_source(context: AccessContext, source_id: UUID) -> None:
    if context.source_ids is not None and source_id not in context.source_ids:
        raise PermissionDeniedException(
            detail="API key does not grant this source.",
            extra={"code": "source_forbidden"},
        )
