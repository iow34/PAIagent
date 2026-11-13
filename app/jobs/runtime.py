"""Database leases, live authorization and tenant-wide admission reservations."""

from dataclasses import dataclass
from datetime import UTC, timedelta
from uuid import UUID, uuid4

import msgspec
from litestar.exceptions import HTTPException, PermissionDeniedException
from sqlalchemy import func, select, text

from app.capabilities.types import ProfileInput
from app.db.models import (
    AnalysisRun,
    AnalysisTenantUsage,
    AnalysisTrigger,
    IntegrationCollection,
    ServiceAPIKey,
)
from app.security.context import AccessContext
from app.security.permissions import require_scope, require_source

ACTIVE = ("pending", "running")
TERMINAL = ("succeeded", "failed", "cancelled")


@dataclass(frozen=True)
class TenantLimits:
    running: int = 2
    pending: int = 100
    triggers: int = 100
    daily_attempts: int = 200
    daily_calls: int = 3200
    daily_input_tokens: int = 10000000
    daily_output_tokens: int = 2000000
    records: int = 5000
    fragments: int = 1000
    model_calls: int = 16
    input_tokens: int = 128000
    output_tokens: int = 16000
    seconds: int = 40


LIMITS = TenantLimits()


class LeaseLost(RuntimeError):
    """No result is allowed from this attempt, including after cancellation."""


async def db_now(session):
    return (await session.scalar(select(func.clock_timestamp()))).astimezone(UTC)


async def queue_lock(session):
    # Short admission/claim bookkeeping only; never held during provider work.
    await session.execute(text("SELECT pg_advisory_xact_lock(22589877425259091)"))


def clamp(config: ProfileInput, limits: TenantLimits) -> ProfileInput:
    b = config.budget
    budget = msgspec.structs.replace(
        b,
        max_records=min(b.max_records, limits.records),
        max_fragments=min(b.max_fragments, limits.fragments),
        model_calls=min(b.model_calls, limits.model_calls),
        input_tokens=min(b.input_tokens, limits.input_tokens),
        output_tokens=min(b.output_tokens, limits.output_tokens),
        seconds=min(b.seconds, limits.seconds),
    )
    return msgspec.structs.replace(config, budget=budget)


def cap_sources(original, current):
    if original is None:
        return current
    if current is None:
        return original
    return sorted(set(original) & set(current))


async def authorize(session, row):
    key = await session.get(ServiceAPIKey, row.key_id) if row.key_id else None
    if key is None or not key.is_active or key.tenant_id != row.tenant_id:
        raise PermissionDeniedException(
            detail="Execution key is revoked or unavailable.",
            extra={"code": "execution_forbidden"},
        )
    sources = cap_sources(row.grant_sources, key.source_ids)
    context = AccessContext(
        row.tenant_id,
        row.user_id,
        tuple(sorted(set(row.grant_scopes) & set(key.scopes))),
        None if sources is None else tuple(UUID(s) for s in sources),
        key.id,
    )
    required = (
        row.required_scopes
        if isinstance(row, AnalysisRun)
        else ["ai:run", "runs:write", "data:read"]
    )
    for scope in required:
        require_scope(context, scope)
    collections = list(
        await session.scalars(
            select(IntegrationCollection).where(
                IntegrationCollection.tenant_id == row.tenant_id,
                IntegrationCollection.id.in_([UUID(cid) for cid in row.collection_ids]),
            )
        )
    )
    if len(collections) != len(set(row.collection_ids)):
        raise PermissionDeniedException(
            detail="An execution collection is unavailable."
        )
    for collection in collections:
        require_source(context, collection.source_id)
    return context


async def reserve(session, row, config, limits, now):
    running = await session.scalar(
        select(func.count())
        .select_from(AnalysisRun)
        .where(AnalysisRun.tenant_id == row.tenant_id, AnalysisRun.status == "running")
    )
    if running >= limits.running:
        return "concurrency_limit"
    usage = await session.get(AnalysisTenantUsage, (row.tenant_id, now.date()))
    if usage is None:
        usage = AnalysisTenantUsage(
            tenant_id=row.tenant_id,
            day=now.date(),
            attempts=0,
            calls=0,
            input_tokens=0,
            output_tokens=0,
        )
        session.add(usage)
    b = config.budget
    if (
        usage.attempts + 1 > limits.daily_attempts
        or usage.calls + b.model_calls > limits.daily_calls
        or usage.input_tokens + b.input_tokens > limits.daily_input_tokens
        or usage.output_tokens + b.output_tokens > limits.daily_output_tokens
    ):
        return "daily_budget"
    usage.attempts += 1
    usage.calls += b.model_calls
    usage.input_tokens += b.input_tokens
    usage.output_tokens += b.output_tokens
    usage.last_claimed_at = now
    row.status = "running"
    row.attempts += 1
    row.started_at = now
    row.finished_at = None
    row.error_code = None
    row.lease_token = uuid4()
    row.lease_expires_at = now + timedelta(seconds=90)
    return None


@dataclass(frozen=True)
class Attempt:
    run_id: UUID
    token: UUID
    limits: TenantLimits = LIMITS

    async def check(self, repo):
        row = await repo.session.scalar(
            select(AnalysisRun)
            .where(
                AnalysisRun.id == self.run_id,
                AnalysisRun.tenant_id == repo.context.tenant_id,
                AnalysisRun.user_id == repo.context.user_id,
            )
            .with_for_update()
        )
        now = await db_now(repo.session)
        if (
            row is None
            or row.status != "running"
            or row.lease_token != self.token
            or row.lease_expires_at <= now
        ):
            raise LeaseLost("Attempt no longer owns this run.")
        await authorize(repo.session, row)
        if row.trigger_id:
            trigger = await repo.session.get(AnalysisTrigger, row.trigger_id)
            if (
                trigger is None
                or not trigger.enabled
                or trigger.revision != row.trigger_revision
            ):
                raise LeaseLost("Trigger configuration changed.")
        return row, now

    async def start(self, repo, signature):
        row, now = await self.check(repo)
        row.input_boundary = signature
        row.data_as_of = now
        return now

    async def succeed(self, repo):
        row, now = await self.check(repo)
        row.status = "succeeded"
        row.finished_at = now
        row.lease_token = None
        row.lease_expires_at = None


def quota_error(code):
    raise HTTPException(
        status_code=429,
        detail="Tenant execution quota reached.",
        extra={"code": code},
        headers={"Retry-After": "5"},
    )
