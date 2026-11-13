"""Durable outbox consumption and drift-free interval scheduling."""

from datetime import UTC, timedelta
from uuid import uuid4

import msgspec
from litestar.exceptions import HTTPException
from sqlalchemy import func, select, update

from app.capabilities.context import snapshot
from app.capabilities.types import RunInput
from app.db.models import AnalysisRun, AnalysisTrigger
from app.integration.validation import ContractError
from app.jobs.runtime import ACTIVE, LIMITS, authorize, db_now, queue_lock, quota_error
from app.jobs.types import TriggerInput
from app.personalization.context import trigger_signature
from app.repositories.data_lifecycle import DataRepository, lock_owner, owner
from app.security.context import AccessContext
from app.security.permissions import require_scope
from app.services.job_service import JobService, validate_input


def trigger_view(row):
    return {
        "profile_id": str(row.profile_id),
        "config": row.config,
        "enabled": row.enabled,
        "revision": str(row.revision),
        "next_due_at": row.next_due_at,
        "last_checked_at": row.checked_at,
        "error_code": row.error_code,
    }


class TriggerService(JobService):
    async def configure(self, profile_id, data):
        for scope in ("profiles:write", "ai:run", "runs:write", "data:read"):
            require_scope(self.context, scope)
        data = msgspec.convert(msgspec.to_builtins(data), type=TriggerInput)
        if not self.context.api_key_id:
            raise ContractError(
                "Triggers require an authenticated service key identity."
            )
        if data.enabled and not data.on_change and data.interval_seconds is None:
            raise ContractError(
                "An enabled trigger needs on_change or interval_seconds."
            )
        if data.max_wait_seconds < data.debounce_seconds:
            raise ContractError("max_wait_seconds must be at least debounce_seconds.")
        if data.start_at:
            if data.interval_seconds is None or data.start_at.utcoffset() is None:
                raise ContractError(
                    "start_at requires an interval and an aware timestamp."
                )
            try:
                data.start_at.astimezone(UTC) + timedelta(seconds=data.interval_seconds)
            except (ValueError, OverflowError) as error:
                raise ContractError(
                    "Schedule timestamp is outside the supported range."
                ) from error
        async with self.transaction(write=True) as repo:
            _, config = await self.load(repo, profile_id, data.version)
            await self.configuration(repo, config, writable=True)
            validate_input(config, RunInput(data.version, data.query, limit=data.limit))
            await queue_lock(repo.session)
            row = await repo.session.get(AnalysisTrigger, profile_id)
            if row is None:
                count = await repo.session.scalar(
                    select(func.count())
                    .select_from(AnalysisTrigger)
                    .where(AnalysisTrigger.tenant_id == self.context.tenant_id)
                )
                if (count or 0) >= self.limits.triggers:
                    quota_error("trigger_limit")
                row = AnalysisTrigger(
                    profile_id=profile_id,
                    tenant_id=self.context.tenant_id,
                    user_id=self.context.user_id,
                )
            now = await db_now(repo.session)
            await repo.session.execute(
                update(AnalysisRun)
                .where(
                    *owner(AnalysisRun, self.context),
                    AnalysisRun.trigger_id == profile_id,
                    AnalysisRun.status.in_(ACTIVE),
                )
                .values(
                    status="cancelled",
                    error_code="trigger_changed",
                    finished_at=now,
                    lease_token=None,
                    lease_expires_at=None,
                )
            )
            row.profile_version = data.version
            row.key_id = self.context.api_key_id
            row.grant_scopes = list(self.context.scopes)
            row.grant_sources = (
                None
                if self.context.source_ids is None
                else [str(s) for s in self.context.source_ids]
            )
            row.collection_ids = sorted(
                {str(s.collection_id) for s in config.selections}
                | (
                    {str(cid) for cid in config.personalization.collections}
                    if config.personalization.enabled
                    else set()
                )
            )
            row.config = msgspec.to_builtins(data)
            row.revision = uuid4()
            row.enabled = data.enabled
            row.last_signature = await trigger_signature(
                repo,
                self.context,
                config,
                await snapshot(
                    repo, self.context, [s.collection_id for s in config.selections]
                ),
            )
            row.checked_at = now
            row.error_code = None
            row.next_due_at = (
                (data.start_at or now + timedelta(seconds=data.interval_seconds))
                if data.interval_seconds
                else None
            )
            repo.session.add(row)
            await repo.session.flush()
            return trigger_view(row)

    async def get_trigger(self, profile_id):
        require_scope(self.context, "profiles:read")
        async with self.transaction() as repo:
            await self.load(repo, profile_id)
            row = await repo.session.get(AnalysisTrigger, profile_id)
            if row is None:
                from litestar.exceptions import NotFoundException

                raise NotFoundException(detail="Trigger configuration not found.")
            return trigger_view(row)


async def schedule(factory, *, limits=LIMITS, batch_size=50):
    # Candidates are read unlocked. Every mutation uses owner->trigger->queue
    # ordering, matching profile deletion and avoiding a trigger/owner deadlock.
    async with factory() as session:
        candidates = (
            await session.execute(
                select(
                    AnalysisTrigger.profile_id,
                    AnalysisTrigger.tenant_id,
                    AnalysisTrigger.user_id,
                )
                .where(AnalysisTrigger.enabled.is_(True))
                .order_by(
                    AnalysisTrigger.checked_at.asc().nullsfirst(),
                    AnalysisTrigger.profile_id,
                )
                .limit(batch_size)
            )
        ).all()
    scheduled = 0
    for profile_id, tenant_id, user_id in candidates:
        async with factory.begin() as session:
            await lock_owner(session, AccessContext(tenant_id, user_id))
            trigger = await session.scalar(
                select(AnalysisTrigger)
                .where(
                    AnalysisTrigger.profile_id == profile_id,
                    AnalysisTrigger.enabled.is_(True),
                )
                .with_for_update(skip_locked=True)
            )
            if trigger is None:
                continue
            now = await db_now(session)
            trigger.checked_at = now
            try:
                context = await authorize(session, trigger)
                service = TriggerService(factory, context, limits=limits)
                repo = DataRepository(session, context)
                _, config = await service.load(
                    repo, profile_id, trigger.profile_version
                )
                await service.configuration(repo, config, writable=True)
                settings = msgspec.convert(trigger.config, type=TriggerInput)
                signature = await trigger_signature(
                    repo,
                    context,
                    config,
                    await snapshot(
                        repo, context, [s.collection_id for s in config.selections]
                    ),
                )
                changed = settings.on_change and signature != trigger.last_signature
                due = trigger.next_due_at is not None and trigger.next_due_at <= now
                if not changed and not due:
                    continue
                await queue_lock(session)
                pending = await session.scalar(
                    select(AnalysisRun)
                    .where(
                        AnalysisRun.trigger_id == profile_id,
                        AnalysisRun.status == "pending",
                    )
                    .with_for_update()
                )
                if pending:
                    if changed:
                        pending.available_at = min(
                            now + timedelta(seconds=settings.debounce_seconds),
                            pending.created_at
                            + timedelta(seconds=settings.max_wait_seconds),
                        )
                else:
                    pending, _, _ = await service._admit(
                        repo,
                        profile_id,
                        RunInput(
                            settings.version, settings.query, limit=settings.limit
                        ),
                        reason="change" if changed else "schedule",
                        trigger=trigger,
                    )
                    if changed and not due:
                        pending.available_at = now + timedelta(
                            seconds=settings.debounce_seconds
                        )
                    scheduled += 1
                trigger.last_signature = signature
                trigger.error_code = None
                due_at = trigger.next_due_at
                interval = settings.interval_seconds
                if due and due_at is not None and interval is not None:
                    ticks = (now - due_at).total_seconds() // interval + 1
                    trigger.next_due_at = due_at + timedelta(
                        seconds=int(ticks) * interval
                    )

            except HTTPException as error:
                trigger.error_code = (
                    "queue_limit" if error.status_code == 429 else "execution_forbidden"
                )
                if error.status_code != 429:
                    trigger.enabled = False
            except ContractError:
                trigger.enabled = False
                trigger.error_code = "invalid_trigger"
    return scheduled
