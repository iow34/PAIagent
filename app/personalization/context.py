"""Bounded optional context. Preferences are never promoted to factual evidence."""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from litestar.exceptions import HTTPException
from sqlalchemy import select

from app.db.models import (
    AnalysisResult,
    IntegrationCollection,
    MemoryItem,
    ResultFeedback,
    ServiceAPIKey,
)
from app.integration.validation import ContractError
from app.jobs.runtime import cap_sources
from app.personalization.lifecycle import settings_row, source_current
from app.personalization.validation import proposal_fingerprint, validate_value
from app.repositories.data_lifecycle import check_fence, owner
from app.security.context import AccessContext
from app.security.permissions import require_source
from app.services.data_service import digest


def empty(status="disabled") -> dict[str, Any]:
    return {
        "status": status,
        "revision": None,
        "items": [],
        "rejected": [],
        "feedback_records": 0,
        "feedback_partial": False,
    }


async def effective_context(session, context):
    if context.api_key_id is None:
        return context
    key = await session.get(ServiceAPIKey, context.api_key_id)
    if key is None or not key.is_active or key.tenant_id != context.tenant_id:
        return None
    current = (
        None if context.source_ids is None else [str(cid) for cid in context.source_ids]
    )
    sources = cap_sources(current, key.source_ids)
    return AccessContext(
        context.tenant_id,
        context.user_id,
        tuple(set(context.scopes) & set(key.scopes)),
        None if sources is None else tuple(UUID(cid) for cid in sources),
        key.id,
    )


async def load_context(repo, context, config, profile_id):
    policy = config.personalization
    if not policy.enabled:
        return empty()
    live = await effective_context(repo.session, context)
    if live is None or not ({"memory:read", "feedback:read"} & set(live.scopes)):
        return empty("not_authorized")
    settings = await settings_row(repo.session, context)
    if settings is None or not settings.enabled:
        return empty()
    allowed = set(settings.config["collections"]) & {
        str(cid) for cid in policy.collections
    }
    result = empty("active")
    result["revision"] = str(settings.revision)
    if "memory:read" in live.scopes:
        # Read a bounded owner-local candidate pool; confirmable candidates and
        # invalidated/deleted values never enter generation.
        rows = list(
            await repo.session.scalars(
                select(MemoryItem)
                .where(
                    *owner(MemoryItem, context),
                    MemoryItem.collection_id.in_([UUID(cid) for cid in allowed]),
                    MemoryItem.key.in_(policy.keys),
                    MemoryItem.state == "confirmed",
                )
                .order_by(MemoryItem.updated_at.desc(), MemoryItem.id)
                .limit(2000)
            )
        )
        candidates = []
        for row in rows:
            try:
                require_source(live, row.source_id)
                await check_fence(
                    repo.session,
                    context,
                    source_id=row.source_id,
                    collection_id=row.collection_id,
                )
                validate_value(settings.config, row.key, row.value)
            except (ContractError, HTTPException):
                continue
            if row.kind == "observation":
                if not policy.observations:
                    continue
                periods = [
                    p
                    for p in [config.period, config.comparison_period]
                    if p is not None
                ]
                if periods:
                    if not row.period:
                        continue
                    start = datetime.fromisoformat(row.period["start"])
                    end = datetime.fromisoformat(row.period["end"])
                    if not any(start < p.end and p.start < end for p in periods):
                        continue
            if not await source_current(repo.session, live, row):
                continue
            # Explicit preference priority; key order is the profile's deterministic relevance order.
            candidates.append(row)
        candidates.sort(
            key=lambda row: (
                policy.keys.index(row.key),
                row.kind != "preference",
                row.origin != "explicit",
                str(row.id),
            )
        )
        size = 0
        for row in candidates:
            item = {
                "id": str(row.id),
                "version": row.version,
                "collection_id": str(row.collection_id),
                "key": row.key,
                "kind": row.kind,
                "value": row.value,
                "period": row.period,
                "provenance": row.provenance,
            }
            cost = len(json.dumps(item, ensure_ascii=False))
            if size + cost > policy.max_chars:
                continue
            result["items"].append(item)
            size += cost
            if len(result["items"]) >= policy.max_items:
                break
    if policy.use_feedback and "feedback:read" in live.scopes:
        rows = (
            await repo.session.execute(
                select(ResultFeedback, AnalysisResult.collection_ids)
                .join(AnalysisResult, AnalysisResult.id == ResultFeedback.result_id)
                .where(
                    *owner(ResultFeedback, context),
                    *owner(AnalysisResult, context),
                    ResultFeedback.profile_id == profile_id,
                    ResultFeedback.decision.is_not(None),
                )
                .order_by(ResultFeedback.revision.desc())
                .limit(5001)
            )
        ).all()
        authorized = None
        if live.source_ids is not None:
            cids = list({UUID(cid) for _, collections in rows for cid in collections})
            authorized = set()
            for offset in range(0, len(cids), 1000):
                authorized.update(
                    str(cid)
                    for cid in await repo.session.scalars(
                        select(IntegrationCollection.id).where(
                            IntegrationCollection.tenant_id == context.tenant_id,
                            IntegrationCollection.source_id.in_(live.source_ids),
                            IntegrationCollection.id.in_(cids[offset : offset + 1000]),
                        )
                    )
                )
        latest = {}
        inspected = 0
        for row, collections in rows[:5000]:
            if authorized is not None and not set(collections) <= authorized:
                continue
            inspected += 1
            for fingerprint in row.proposal_fingerprints:
                latest.setdefault(fingerprint, row.decision)
        result["rejected"] = [
            fp
            for fp, decision in latest.items()
            if decision in {"rejected", "outdated"}
        ]
        result["feedback_records"] = inspected
        result["feedback_partial"] = len(rows) > 5000
    return result


async def current_revision(repo, context):
    row = await settings_row(repo.session, context)
    return str(row.revision) if row else None


async def trigger_signature(repo, context, config, base):
    if not config.personalization.enabled:
        return base
    row = await settings_row(repo.session, context)
    marker = str(row.revision) if row is not None and row.enabled else "disabled"
    return digest([base, marker])


def public_context(context):
    return {
        "status": context["status"],
        "revision": context["revision"],
        "memory": [
            {"id": item["id"], "version": item["version"]} for item in context["items"]
        ],
        "feedback_records": context["feedback_records"],
        "feedback_partial": context["feedback_partial"],
        "suppressed_recommendations": 0,
    }


def suppress_repeats(config, result, context):
    if config.capability != "recommend" or not context["rejected"]:
        return 0
    rejected = set(context["rejected"])
    claims = result["payload"].get("claims", [])
    allowed = [
        claim for claim in claims if proposal_fingerprint(claim["text"]) not in rejected
    ]
    count = len(claims) - len(allowed)
    if count:
        result["payload"]["claims"] = allowed
        result["payload"]["limitations"].append(
            "Repeated recommendations were withheld according to explicit result feedback."
        )
        result["data_state"] = "partial" if allowed else "insufficient_data"
    return count
