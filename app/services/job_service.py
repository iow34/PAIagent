"""Durable run admission, bounded retry/recovery and explicit polling API."""

import asyncio
from datetime import UTC, datetime, time, timedelta

import msgspec
from litestar.exceptions import HTTPException
from sqlalchemy import func, select

from app.capabilities.types import Budget, ProfileInput, RunInput
from app.db.models import (
    AnalysisProfileVersion,
    AnalysisResult,
    AnalysisRun,
    AnalysisTenantUsage,
)
from app.integration.validation import ContractError, bounded_json
from app.jobs.runtime import (
    ACTIVE,
    LIMITS,
    Attempt,
    LeaseLost,
    authorize,
    clamp,
    db_now,
    queue_lock,
    quota_error,
    reserve,
)
from app.jobs.types import CreateRun
from app.repositories.data_lifecycle import owner
from app.security.permissions import require_scope
from app.services.capability_service import CapabilityService
from app.services.data_service import conflict, digest
from app.services.integration_service import found


def validate_input(config, data, *, ephemeral=False):
    if data.inline and not ephemeral:
        raise ContractError(
            "Durable runs accept collection input only; inline remains ephemeral in the synchronous profile API."
        )
    if not config.selections and not ephemeral:
        raise ContractError("Durable runs require collection selections.")
    if bool(data.inline) == bool(config.selections):
        raise ContractError("Choose explicit inline input or collection selections.")
    if data.history and config.capability != "query":
        raise ContractError("History is supported by query only.")
    if config.capability in {"search", "query"} and not data.query.strip():
        raise ContractError("search/query require a non-blank query.")
    bounded_json(msgspec.to_builtins(data), 1048576)


class JobService(CapabilityService):
    def __init__(self, factory, context, *, limits=LIMITS):
        super().__init__(factory, context)
        self.limits = limits

    async def _admit(
        self,
        repo,
        profile_id,
        data,
        *,
        idempotency_key=None,
        reason="manual",
        trigger=None,
        synchronous=False,
        ephemeral=False,
    ):
        require_scope(self.context, "ai:run")
        if not self.context.api_key_id:
            raise ContractError(
                "Durable execution requires an authenticated service key identity."
            )
        _, config = await self.load(repo, profile_id, data.version)
        await self.configuration(repo, config, writable=True)
        if config.selections:
            require_scope(self.context, "data:read")
        validate_input(config, data, ephemeral=ephemeral)
        config = clamp(config, self.limits)
        if ephemeral:
            config = msgspec.structs.replace(
                config, budget=msgspec.structs.replace(config.budget, max_attempts=1)
            )
        fingerprint = digest([str(profile_id), data])
        if idempotency_key is not None:
            existing = await repo.session.scalar(
                select(AnalysisRun).where(
                    *owner(AnalysisRun, self.context),
                    AnalysisRun.idempotency_key == idempotency_key,
                )
            )
            if existing:
                if existing.fingerprint != fingerprint:
                    conflict(
                        "run_idempotency_conflict",
                        "The idempotency key already identifies another immutable request.",
                    )
                await self.load(repo, existing.profile_id, existing.profile_version)
                return existing, config, True
        await queue_lock(repo.session)
        queued = await repo.session.scalar(
            select(func.count())
            .select_from(AnalysisRun)
            .where(
                AnalysisRun.tenant_id == self.context.tenant_id,
                AnalysisRun.status.in_(ACTIVE),
            )
        )
        if queued >= self.limits.pending:
            quota_error("queue_limit")
        now = await db_now(repo.session)
        required = ["ai:run"] + (["data:read"] if config.selections else [])
        if reason != "sync":
            required.append("runs:write")
        row = AnalysisRun(
            tenant_id=self.context.tenant_id,
            user_id=self.context.user_id,
            profile_id=profile_id,
            profile_version=data.version,
            key_id=self.context.api_key_id,
            grant_scopes=list(self.context.scopes),
            grant_sources=None
            if self.context.source_ids is None
            else [str(s) for s in self.context.source_ids],
            collection_ids=[str(s.collection_id) for s in config.selections],
            required_scopes=required,
            trigger_id=trigger.profile_id if trigger else None,
            trigger_revision=trigger.revision if trigger else None,
            input={} if ephemeral else msgspec.to_builtins(data),
            budget=msgspec.to_builtins(config.budget),
            fingerprint="" if ephemeral else fingerprint,
            idempotency_key=idempotency_key,
            reason=reason,
            status="pending",
            attempts=0,
            max_attempts=1 if ephemeral else config.budget.max_attempts,
            retry_seconds=config.budget.retry_seconds,
            ephemeral=ephemeral,
            available_at=now,
            created_at=now,
        )
        if synchronous:
            error = await reserve(repo.session, row, config, self.limits, now)
            if error:
                quota_error(error)
        await repo.add(row)
        return row, config, False

    async def enqueue(self, request: CreateRun):
        require_scope(self.context, "runs:write")
        request = msgspec.convert(msgspec.to_builtins(request), type=CreateRun)
        if (
            not request.idempotency_key.strip()
            or not request.idempotency_key.isprintable()
        ):
            raise ContractError("idempotency_key must be printable and non-blank.")
        async with self.transaction(write=True) as repo:
            row, _, _ = await self._admit(
                repo,
                request.profile_id,
                request.input,
                idempotency_key=request.idempotency_key,
            )
            return await self.view(repo, row)

    async def view(self, repo, row):
        result_id = await repo.session.scalar(
            select(AnalysisResult.id).where(AnalysisResult.run_id == row.id)
        )
        return {
            "id": str(row.id),
            "profile_id": str(row.profile_id),
            "profile_version": row.profile_version,
            "status": row.status,
            "reason": row.reason,
            "attempts": row.attempts,
            "max_attempts": row.max_attempts,
            "budget": row.budget,
            "lease_expires_at": row.lease_expires_at,
            "created_at": row.created_at,
            "available_at": row.available_at,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "input_boundary": row.input_boundary,
            "data_as_of": row.data_as_of,
            "result_id": str(result_id) if result_id else None,
            "error_code": row.error_code,
            "ephemeral": row.ephemeral,
        }

    async def visible(self, repo, run_id):
        row = found(
            await repo.session.scalar(
                select(AnalysisRun).where(
                    *owner(AnalysisRun, self.context), AnalysisRun.id == run_id
                )
            )
        )
        await self.load(repo, row.profile_id, row.profile_version)
        if row.collection_ids:
            require_scope(self.context, "data:read")
        return row

    async def get_run(self, run_id):
        require_scope(self.context, "runs:read")
        return await self.created_view(run_id)

    async def created_view(self, run_id):
        async with self.transaction() as repo:
            return await self.view(repo, await self.visible(repo, run_id))

    async def runs(self, cursor=None, limit=50, status=None, profile_id=None):
        require_scope(self.context, "runs:read")
        async with self.transaction() as repo:
            stmt = select(AnalysisRun).where(*owner(AnalysisRun, self.context))
            if cursor:
                stmt = stmt.where(AnalysisRun.id > cursor)
            if status:
                stmt = stmt.where(AnalysisRun.status == status)
            if profile_id:
                await self.load(repo, profile_id)
                stmt = stmt.where(AnalysisRun.profile_id == profile_id)
            rows = list(
                await repo.session.scalars(
                    stmt.order_by(AnalysisRun.id).limit(limit + 1)
                )
            )
            items = []
            for row in rows[:limit]:
                try:
                    await self.visible(repo, row.id)
                except HTTPException as error:
                    if error.status_code in {403, 404, 410}:
                        continue
                    raise
                items.append(await self.view(repo, row))
            return {
                "items": items,
                "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
            }

    async def cancel(self, run_id):
        require_scope(self.context, "runs:write")
        async with self.transaction(write=True) as repo:
            row = await self.visible(repo, run_id)
            if row.status in ACTIVE:
                row.status, row.error_code = "cancelled", "user_cancelled"
                row.lease_token = row.lease_expires_at = None
                row.finished_at = await db_now(repo.session)
            return await self.view(repo, row)

    async def recover(self, session, now):
        rows = list(
            await session.scalars(
                select(AnalysisRun)
                .where(
                    AnalysisRun.status == "running", AnalysisRun.lease_expires_at <= now
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            )
        )
        for row in rows:
            await self._failure(session, row, now, "worker_lost", retry=True)

    async def _failure(self, session, row, now, code, *, retry):
        successor = (
            await session.scalar(
                select(AnalysisRun.id).where(
                    AnalysisRun.trigger_id == row.trigger_id,
                    AnalysisRun.id != row.id,
                    AnalysisRun.status == "pending",
                )
            )
            if row.trigger_id
            else None
        )
        row.error_code = code
        row.lease_token = row.lease_expires_at = None
        if successor:
            row.status, row.error_code = "cancelled", "superseded"
        elif retry and row.attempts < row.max_attempts and not row.ephemeral:
            row.status = "pending"
            row.available_at = now + timedelta(
                seconds=min(300, row.retry_seconds * 2 ** max(0, row.attempts - 1))
            )
            return
        else:
            row.status = "failed"
        row.finished_at = now

    async def claim(self, run_id=None):
        # No owner locks here: queue bookkeeping must not invert owner->queue order.
        async with self.factory.begin() as session:
            await queue_lock(session)
            now = await db_now(session)
            await self.recover(session, now)
            running = (
                select(AnalysisRun.tenant_id.label("tenant"), func.count().label("n"))
                .where(AnalysisRun.status == "running")
                .group_by(AnalysisRun.tenant_id)
                .subquery()
            )
            usage = AnalysisTenantUsage
            stmt = (
                select(AnalysisRun)
                .outerjoin(running, running.c.tenant == AnalysisRun.tenant_id)
                .outerjoin(
                    usage,
                    (usage.tenant_id == AnalysisRun.tenant_id)
                    & (usage.day == now.date()),
                )
                .where(
                    AnalysisRun.status == "pending",
                    AnalysisRun.available_at <= now,
                    func.coalesce(running.c.n, 0) < self.limits.running,
                )
            )
            if run_id:
                stmt = stmt.where(AnalysisRun.id == run_id)
            rows = list(
                await session.scalars(
                    stmt.order_by(
                        usage.last_claimed_at.asc().nullsfirst(),
                        AnalysisRun.available_at,
                        AnalysisRun.id,
                    )
                    .with_for_update(skip_locked=True, of=AnalysisRun)
                    .limit(100)
                )
            )
            for row in rows:
                try:
                    context = await authorize(session, row)
                    version = found(
                        await session.get(
                            AnalysisProfileVersion,
                            (row.profile_id, row.profile_version),
                        )
                    )
                    config = clamp(
                        msgspec.structs.replace(
                            msgspec.convert(version.config, type=ProfileInput),
                            budget=msgspec.convert(row.budget, type=Budget),
                        ),
                        self.limits,
                    )
                    # Source/key checks above; data fences are rechecked under owner lock at execution.
                    error = await reserve(session, row, config, self.limits, now)
                    if error == "daily_budget":
                        row.available_at = datetime.combine(
                            now.date() + timedelta(days=1), time(), UTC
                        )
                        row.error_code = error
                        continue
                    if error:
                        continue
                    await session.flush()
                    return row.id, row.lease_token, context, config
                except HTTPException:
                    await self._failure(
                        session, row, now, "execution_forbidden", retry=False
                    )
            return None

    async def fail(self, attempt, code, *, retry=False):
        async with self.factory.begin() as session:
            await queue_lock(session)
            row = await session.scalar(
                select(AnalysisRun)
                .where(AnalysisRun.id == attempt.run_id)
                .with_for_update()
            )
            now = await db_now(session)
            if (
                row is not None
                and row.status == "running"
                and row.lease_token == attempt.token
                and row.lease_expires_at > now
            ):
                await self._failure(session, row, now, code, retry=retry)

    async def execute(
        self, claim, generator, embeddings, *, data=None, propagate=False
    ):
        run_id, token, context, config = claim
        attempt = Attempt(run_id, token, self.limits)
        async with self.factory() as session:
            row = await session.get(AnalysisRun, run_id)
            if row is None or row.status != "running" or row.lease_token != token:
                return None
            data = data or msgspec.convert(row.input, type=RunInput)

        async def work():
            async with asyncio.timeout(config.budget.seconds + 10):
                return await CapabilityService(self.factory, context).run(
                    row.profile_id, data, generator, embeddings, attempt=attempt
                )

        async def monitor():
            while True:
                await asyncio.sleep(0.25)
                async with self.factory() as session:
                    current = await session.get(AnalysisRun, run_id)
                    now = await db_now(session)
                    if current is not None and current.status == "succeeded":
                        return
                    if (
                        current is None
                        or current.status != "running"
                        or current.lease_token != token
                        or current.lease_expires_at <= now
                    ):
                        raise LeaseLost("Run was cancelled, recovered or erased.")

        task, watcher = asyncio.create_task(work()), asyncio.create_task(monitor())
        try:
            done, _ = await asyncio.wait(
                [task, watcher], return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                return await task
            await watcher
            return await task
        except LeaseLost:
            if propagate:
                raise HTTPException(
                    status_code=409,
                    detail="Execution ownership was cancelled or expired.",
                    extra={"code": "run_cancelled"},
                ) from None
        except (HTTPException, ContractError, TimeoutError) as error:
            code = (
                error.code
                if isinstance(error, ContractError)
                else "attempt_timeout"
                if isinstance(error, TimeoutError)
                else (
                    error.extra.get("code", "execution_failed")
                    if isinstance(error.extra, dict)
                    else "execution_failed"
                )
            )
            retry = isinstance(error, TimeoutError) or (
                isinstance(error, HTTPException)
                and error.status_code in {409, 502, 503, 504}
            )
            await self.fail(attempt, code, retry=retry and not propagate)
            if propagate:
                raise
        except Exception:
            await self.fail(attempt, "internal_error", retry=False)
            if propagate:
                raise
        finally:
            task.cancel()
            watcher.cancel()
            await asyncio.gather(task, watcher, return_exceptions=True)
        return None

    async def sync(self, profile_id, data, generator, embeddings):
        # Backward-compatible synchronous profile endpoint, same tenant admission.
        async with self.transaction(write=True) as repo:
            row, config, _ = await self._admit(
                repo,
                profile_id,
                data,
                reason="sync",
                synchronous=True,
                ephemeral=bool(data.inline),
            )
            claim = row.id, row.lease_token, self.context, config
        return await self.execute(
            claim, generator, embeddings, data=data, propagate=True
        )
