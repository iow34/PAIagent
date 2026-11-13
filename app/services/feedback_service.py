"""Explicit, owner-authorized result feedback with a documented deterministic effect."""

from uuid import UUID

import msgspec
from litestar.exceptions import HTTPException
from sqlalchemy import delete, select

from app.db.models import AnalysisProfileVersion, AnalysisResult, ResultFeedback
from app.integration.validation import ContractError, bounded_json
from app.personalization.lifecycle import touch
from app.personalization.types import FeedbackInput
from app.personalization.validation import proposal_fingerprint
from app.repositories.data_lifecycle import owner
from app.security.permissions import require_scope
from app.services.data_service import conflict, digest
from app.services.integration_service import found
from app.services.memory_service import MemoryService

MAX_FEEDBACK = 5000


def feedback_view(row):
    return {
        "id": str(row.id),
        "result_id": str(row.result_id),
        "profile_id": str(row.profile_id),
        "request_id": row.request_id,
        "rating": row.rating,
        "decision": row.decision,
        "claim_index": row.claim_index,
        "comment": row.comment,
        "revision": row.revision,
        "created_at": row.created_at,
        "effect": "Exact normalized recommendation repeats are suppressed by the latest decision within this profile; acceptance is not execution.",
    }


class FeedbackService(MemoryService):
    async def feedback_result(self, repo, result_id, *, write=False):
        require_scope(self.context, "data:read")
        row = found(
            await repo.session.scalar(
                select(AnalysisResult).where(
                    *owner(AnalysisResult, self.context), AnalysisResult.id == result_id
                )
            )
        )
        for cid in row.collection_ids:
            await self.collection(repo, UUID(cid), writable=write)
        return row

    async def submit(self, result_id, data: FeedbackInput):
        require_scope(self.context, "feedback:write")
        data = msgspec.convert(msgspec.to_builtins(data), type=FeedbackInput)
        bounded_json(msgspec.to_builtins(data), 16384)
        if not data.request_id.strip() or not data.request_id.isprintable():
            raise ContractError("Feedback request ID must be printable and non-blank.")
        if (
            data.rating is None
            and data.decision is None
            and data.preference is None
            and not data.comment.strip()
        ):
            raise ContractError(
                "Feedback needs a rating, decision, comment or explicit preference."
            )
        async with self.transaction(write=True) as repo:
            result = await self.feedback_result(repo, result_id, write=True)
            fingerprint = digest([str(result_id), data])
            existing = await repo.session.scalar(
                select(ResultFeedback).where(
                    *owner(ResultFeedback, self.context),
                    ResultFeedback.request_id == data.request_id,
                )
            )
            if existing:
                if existing.fingerprint != fingerprint:
                    conflict("feedback_conflict", "Feedback request ID is immutable.")
                return feedback_view(existing)
            await self._capacity(repo, ResultFeedback, MAX_FEEDBACK)
            claims = result.envelope["payload"].get("claims", [])
            if data.claim_index is not None and data.claim_index >= len(claims):
                raise ContractError("Feedback claim index is unavailable.")
            fingerprints = []
            if result.envelope["capability"] == "recommend" and data.decision:
                selected = (
                    claims if data.claim_index is None else [claims[data.claim_index]]
                )
                fingerprints = list(
                    dict.fromkeys(proposal_fingerprint(c["text"]) for c in selected)
                )
            if data.preference:
                require_scope(self.context, "memory:write")
                profile = found(
                    await repo.session.get(
                        AnalysisProfileVersion,
                        (result.profile_id, result.profile_version),
                    )
                )
                allowed = set(result.collection_ids) | set(
                    profile.config.get("personalization", {}).get("collections", [])
                )
                if str(data.preference.collection_id) not in allowed:
                    raise ContractError(
                        "Feedback preference must be scoped to a collection of this result."
                    )
                await self._preference(repo, data.preference)
            revision = await touch(repo.session, self.context)
            row = await repo.add(
                ResultFeedback(
                    tenant_id=self.context.tenant_id,
                    user_id=self.context.user_id,
                    result_id=result.id,
                    profile_id=result.profile_id,
                    request_id=data.request_id,
                    fingerprint=fingerprint,
                    revision=revision.version,
                    rating=data.rating,
                    decision=data.decision,
                    claim_index=data.claim_index,
                    proposal_fingerprints=fingerprints,
                    comment=data.comment,
                )
            )
            return feedback_view(row)

    async def feedback(self, result_id, cursor=None, limit=50):
        require_scope(self.context, "feedback:read")
        async with self.transaction() as repo:
            await self.feedback_result(repo, result_id)
            stmt = select(ResultFeedback).where(
                *owner(ResultFeedback, self.context),
                ResultFeedback.result_id == result_id,
            )
            if cursor:
                stmt = stmt.where(ResultFeedback.id > cursor)
            rows = list(
                await repo.session.scalars(
                    stmt.order_by(ResultFeedback.id).limit(limit + 1)
                )
            )
            return {
                "items": [feedback_view(row) for row in rows[:limit]],
                "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
            }

    async def delete_feedback(self, result_id):
        require_scope(self.context, "feedback:write")
        async with self.transaction(write=True) as repo:
            await self.feedback_result(repo, result_id, write=True)
            await repo.session.execute(
                delete(ResultFeedback).where(
                    *owner(ResultFeedback, self.context),
                    ResultFeedback.result_id == result_id,
                )
            )
            await touch(repo.session, self.context)
        return {
            "deleted": True,
            "scope": "all feedback records for this result; explicit memory preferences are managed separately",
        }

    async def profile_scores(self):
        require_scope(self.context, "feedback:read")
        require_scope(self.context, "data:read")
        async with self.transaction() as repo:
            rows = list(
                await repo.session.scalars(
                    select(ResultFeedback)
                    .where(*owner(ResultFeedback, self.context))
                    .order_by(ResultFeedback.revision.desc())
                    .limit(1001)
                )
            )
            latest = {}
            for row in rows[:1000]:
                key = (row.result_id, row.claim_index)
                if key in latest:
                    continue
                try:
                    await self.feedback_result(repo, row.result_id)
                except HTTPException as error:
                    if error.status_code in {403, 404, 410}:
                        continue
                    raise
                latest[key] = row
            profiles = {}
            for row in latest.values():
                key = str(row.profile_id)
                item = profiles.setdefault(
                    key,
                    {
                        "profile_id": key,
                        "helpful": 0,
                        "unhelpful": 0,
                        "accepted": 0,
                        "rejected": 0,
                        "outdated": 0,
                        "score": 0,
                    },
                )
                for label in (row.rating, row.decision):
                    if label:
                        item[label] += 1
                        item["score"] += 1 if label in {"helpful", "accepted"} else -1
            return {
                "items": sorted(
                    profiles.values(),
                    key=lambda row: (-row["score"], row["profile_id"]),
                ),
                "partial": len(rows) > 1000,
                "method": "Latest feedback per result/claim target: helpful+accepted minus unhelpful+rejected+outdated. Heuristic, not confidence; caller still explicitly selects a profile.",
            }
