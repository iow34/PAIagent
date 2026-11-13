"""Versioned profiles and synchronous bounded capability execution.

Only successful collected-data runs are persisted. Provider calls happen outside
transactions; an owner-locked snapshot check prevents stale or erased saves.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import msgspec
from litestar.exceptions import HTTPException
from openai import OpenAIError
from sqlalchemy import delete, func, select

from app.capabilities.context import collect, snapshot
from app.capabilities.generation import GenerationError, Meter
from app.capabilities.metrics import aggregate, in_period
from app.capabilities.results import validate_result
from app.capabilities.types import Budget, CapabilityResult, ProfileInput, RunInput
from app.capabilities.validation import validate_profile, validate_selection
from app.capabilities.workflow import CapabilityWorkflow, lexical_rank
from app.db.models import (
    AnalysisProfile,
    AnalysisProfileVersion,
    AnalysisResult,
    EvidenceFragment,
)
from app.integration.validation import ContractError, bounded_json
from app.repositories.data_lifecycle import check_fence, owner
from app.repositories.integration import IntegrationRepository
from app.security.permissions import require_scope
from app.services.data_service import DataService, conflict
from app.services.integration_service import found


def profile_view(row):
    return {
        "profile_id": str(row.profile_id),
        "version": row.version,
        "config": row.config,
        "created_at": row.created_at.isoformat(),
    }


class CapabilityService(DataService):
    async def configuration(self, repo, config, *, writable=False):
        await check_fence(repo.session, self.context)
        schemas = []
        for index, selection in enumerate(config.selections):
            await self.collection(repo, selection.collection_id, writable=writable)
            schema = found(
                await IntegrationRepository(repo.session, self.context).schema(
                    selection.collection_id, selection.kind, selection.schema_version
                )
            )
            validate_selection(
                selection,
                schema.json_schema,
                schema.field_mappings,
                [m for m in config.metrics if m.selection == index],
            )
            schemas.append(schema)
        return schemas

    async def load(self, repo, profile_id, version=None):
        found(
            await repo.session.scalar(
                select(AnalysisProfile).where(
                    *owner(AnalysisProfile, self.context),
                    AnalysisProfile.id == profile_id,
                )
            )
        )
        stmt = select(AnalysisProfileVersion).where(
            AnalysisProfileVersion.profile_id == profile_id
        )
        if version is not None:
            stmt = stmt.where(AnalysisProfileVersion.version == version)
        row = found(
            await repo.session.scalar(
                stmt.order_by(AnalysisProfileVersion.version.desc()).limit(1)
            )
        )
        config = msgspec.convert(row.config, type=ProfileInput)
        await self.configuration(repo, config)
        return row, config

    async def create_profile(self, data, profile_id=None, version=1):
        require_scope(self.context, "profiles:write")
        data = msgspec.convert(msgspec.to_builtins(data), type=ProfileInput)
        validate_profile(data)
        async with self.transaction(write=True) as repo:
            await self.configuration(repo, data, writable=True)
            if profile_id is None:
                root = await repo.add(
                    AnalysisProfile(
                        tenant_id=self.context.tenant_id, user_id=self.context.user_id
                    )
                )
                profile_id = root.id
            else:
                prior, _ = await self.load(repo, profile_id)
                if version != prior.version + 1:
                    conflict(
                        "profile_version_conflict",
                        "Profile versions must increase by one and are immutable.",
                    )
            row = await repo.add(
                AnalysisProfileVersion(
                    profile_id=profile_id,
                    version=version,
                    config=msgspec.to_builtins(data),
                )
            )
            result = profile_view(row)
        return result

    async def get_profile(self, profile_id, version=None):
        require_scope(self.context, "profiles:read")
        async with self.transaction() as repo:
            row, _ = await self.load(repo, profile_id, version)
            return profile_view(row)

    async def profiles(self, cursor=None, limit=50):
        require_scope(self.context, "profiles:read")
        async with self.transaction() as repo:
            stmt = select(AnalysisProfile).where(*owner(AnalysisProfile, self.context))
            if cursor:
                stmt = stmt.where(AnalysisProfile.id > cursor)
            rows = list(
                await repo.session.scalars(
                    stmt.order_by(AnalysisProfile.id).limit(limit + 1)
                )
            )
            items = []
            for root in rows[:limit]:
                try:
                    row, _ = await self.load(repo, root.id)
                except HTTPException as error:
                    if error.status_code in {403, 404, 410}:
                        continue
                    raise
                items.append(profile_view(row))
            return {
                "items": items,
                "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
            }

    async def delete_profile(self, profile_id):
        require_scope(self.context, "profiles:write")
        async with self.transaction(write=True) as repo:
            await self.load(repo, profile_id)
            await repo.session.execute(
                delete(AnalysisProfile).where(
                    *owner(AnalysisProfile, self.context),
                    AnalysisProfile.id == profile_id,
                )
            )
        return {"deleted": True}

    async def retrieve(self, sources, query, embeddings, meter):
        lexical = lexical_rank(sources, query)
        # File vector ranking only uses explicitly permitted, filtered source IDs.
        # JSON rows use projected fields; other registered text fields cannot leak.
        ids = [UUID(s["id"]) for s in sources if "extraction_coverage" in s]
        vectors = []
        if ids:
            async with self.transaction() as repo:
                indexed = await repo.session.scalar(
                    select(func.count())
                    .select_from(EvidenceFragment)
                    .where(
                        EvidenceFragment.id.in_(ids),
                        EvidenceFragment.embedding.is_not(None),
                        EvidenceFragment.embedding_model == "text-embedding-3-small",
                    )
                )
            if indexed:
                charge = len(query.encode()) + 256
                if (
                    meter.calls >= meter.budget.model_calls
                    or meter.input_tokens + charge > meter.budget.input_tokens
                ):
                    raise ContractError(
                        "Retrieval exceeds the model budget.", code="generation_budget"
                    )
                meter.calls += 1
                meter.input_tokens += charge
                embedded = await embeddings.embed_texts([query])
                async with self.transaction() as repo:
                    vectors = list(
                        await repo.session.scalars(
                            select(EvidenceFragment.id)
                            .where(
                                EvidenceFragment.id.in_(ids),
                                EvidenceFragment.embedding.is_not(None),
                                EvidenceFragment.embedding_model
                                == "text-embedding-3-small",
                            )
                            .order_by(
                                EvidenceFragment.embedding.cosine_distance(
                                    embedded.vectors[0]
                                ),
                                EvidenceFragment.id,
                            )
                            .limit(100)
                        )
                    )
        scores = {}
        by_id = {s["id"]: s for s in sources}
        for ranking in ([s["id"] for s in lexical], [str(v) for v in vectors]):
            for rank, sid in enumerate(ranking, 1):
                scores[sid] = scores.get(sid, 0) + 1 / (60 + rank)
        return [
            by_id[sid] for sid in sorted(scores, key=lambda sid: (-scores[sid], sid))
        ]

    async def run(
        self, profile_id: UUID, data: RunInput, generator, embeddings, *, attempt=None
    ) -> dict[str, Any]:
        require_scope(self.context, "ai:run")
        data = msgspec.convert(msgspec.to_builtins(data), type=RunInput)
        bounded_json(msgspec.to_builtins(data), 1048576)
        async with self.transaction(write=True) as repo:
            _row, config = await self.load(repo, profile_id, data.version)
            if attempt is not None:
                from app.jobs.runtime import clamp

                job, _ = await attempt.check(repo)
                if job.profile_id != profile_id or job.profile_version != data.version:
                    from app.jobs.runtime import LeaseLost

                    raise LeaseLost(
                        "Attempt does not match the pinned profile version."
                    )
                config = clamp(
                    msgspec.structs.replace(
                        config, budget=msgspec.convert(job.budget, type=Budget)
                    ),
                    attempt.limits,
                )
            await self.configuration(repo, config, writable=True)
            if config.selections:
                require_scope(self.context, "data:read")
            if bool(data.inline) == bool(config.selections):
                raise ContractError(
                    "Use collection selections or an inline-only profile with explicit inline input."
                )
            if data.history and config.capability != "query":
                raise ContractError("History is supported by query only.")
            if config.capability in {"search", "query"} and not data.query.strip():
                raise ContractError("search/query require a non-blank query.")
            collections = [s.collection_id for s in config.selections]
            signature = await snapshot(repo, self.context, collections)
            attempt_as_of = (
                await attempt.start(repo, signature) if attempt is not None else None
            )
            schemas = await self.configuration(repo, config)
            if collections:
                samples, sources, coverage = await collect(
                    repo, self.context, config, schemas
                )
            else:
                if sum(len(item.text) for item in data.inline) > 500000 or any(
                    not item.text.strip() for item in data.inline
                ):
                    raise ContractError(
                        "Inline text must be non-blank and total at most 500000 characters."
                    )
                samples, sources, coverage = [], [], []
                for index, item in enumerate(data.inline):
                    for offset in range(0, len(item.text), 1200):
                        if len(sources) >= config.budget.max_fragments:
                            break
                        sources.append(
                            {
                                "id": f"inline:{index}:{offset}",
                                "label": item.label,
                                "content": item.text[offset : offset + 1200],
                                "position": {
                                    "input": index,
                                    "start": offset,
                                    "end": min(offset + 1200, len(item.text)),
                                },
                            }
                        )
                expected = sum((len(i.text) + 1199) // 1200 for i in data.inline)
                coverage = [
                    {
                        "inline_inputs": len(data.inline),
                        "matched_fragments": expected,
                        "selected_fragments": len(sources),
                        "complete_selection": len(sources) == expected,
                    }
                ]
        bounded_json(sources, 4 * 1024 * 1024)
        data_as_of = attempt_as_of or datetime.now(UTC)
        # The whole provider phase has a deadline and no live database transaction.
        meter = Meter(config.budget, generator)
        metrics = aggregate(config, samples, schemas)
        partial = any(not c["complete_selection"] for c in coverage) or any(
            s.get("extraction_coverage", "full") != "full" for s in sources
        )
        insufficient = any(
            sum(in_period(r, selection, period) for r in samples[index])
            < config.minimum_samples
            for index, selection in enumerate(config.selections)
            for period in (
                [config.period, config.comparison_period]
                if config.comparison_period
                else [config.period]
            )
        )
        if data.inline:
            insufficient = len(data.inline) < config.minimum_samples
        all_sources = sources
        try:
            async with asyncio.timeout(config.budget.seconds):
                if not insufficient and config.capability in {"search", "query"}:
                    sources = await self.retrieve(
                        sources, data.query, embeddings, meter
                    )
                    sources = sources[: data.limit]
                    partial = True
                if insufficient:
                    insufficient_payload: dict[str, Any] = {
                        "claims": [],
                        "limitations": ["minimum_samples was not met."],
                    }
                    if config.capability == "search":
                        insufficient_payload = {"items": []}
                    elif config.capability == "analyze":
                        insufficient_payload["metrics"] = metrics
                    elif config.capability == "extract":
                        insufficient_payload.update(data=None, source_ids=[])
                    result: dict[str, Any] = {
                        "payload": insufficient_payload,
                        "data_state": "insufficient_data",
                        "processed_ids": [],
                        "partial": partial,
                    }
                else:
                    result = await CapabilityWorkflow().run(
                        {
                            "config": config,
                            "run": data,
                            "sources": sources,
                            "metrics": metrics,
                            "meter": meter,
                            "partial": partial,
                        }
                    )
        except ContractError:
            raise
        except TimeoutError as error:
            raise HTTPException(
                status_code=504,
                detail="Capability execution timed out.",
                extra={"code": "capability_timeout"},
            ) from error
        except (GenerationError, OpenAIError) as error:
            raise HTTPException(
                status_code=502,
                detail="Provider failed or returned invalid evidence/output.",
                extra={"code": "invalid_generation"},
            ) from error
        now = datetime.now(UTC)
        run_id, result_id = (
            (attempt.run_id if attempt is not None else uuid4()),
            uuid4(),
        )
        processed = set(result["processed_ids"])
        # Include all exact-metric row dependencies as well as all processed text.
        if metrics:
            processed.update(r["id"] for rows in samples for r in rows)
        visible_sources = [s for s in all_sources if s["id"] in processed]
        envelope = {
            "run_id": str(run_id),
            "result_id": str(result_id),
            "capability": config.capability,
            "profile_id": str(profile_id),
            "profile_version": data.version,
            "execution_status": "succeeded",
            "data_state": result["data_state"],
            "payload": result["payload"],
            "sources": visible_sources,
            "period": msgspec.to_builtins(config.period),
            "comparison_period": msgspec.to_builtins(config.comparison_period),
            "timezone": config.timezone,
            "coverage": {
                "selections": coverage,
                "processed_source_count": len(processed),
                "selected_source_count": len(all_sources),
                "text_partial": result["partial"],
                "collection_completeness": "unknown"
                if collections
                else "not_applicable",
            },
            "limitations": [
                "Schema-pinned selections only; history/collection completeness is unknown.",
                "Source IDs are checked; semantic support requires separate quality evaluation.",
                "Recommendations are advisory; managed memory is not enabled.",
                "Search/query rank a bounded sample; JSON uses selected-field lexical matching, indexed files also use cosine RRF.",
            ],
            "generated_at": now.isoformat(),
            "data_as_of": data_as_of.isoformat(),
            "freshness": "current",
            "persisted": bool(collections),
            "usage": {
                "model_calls": meter.calls,
                "input_token_reservation": meter.input_tokens,
                "output_token_reservation": meter.output_tokens,
            },
        }
        envelope = msgspec.to_builtins(msgspec.convert(envelope, type=CapabilityResult))
        try:
            validate_result(config, envelope)
        except GenerationError as error:
            raise HTTPException(
                status_code=502,
                detail="Final payload/evidence validation failed.",
                extra={"code": "invalid_generation"},
            ) from error
        # Persisted envelope is bounded even for many exact-metric provenance IDs.
        bounded_json(envelope, 8 * 1024 * 1024)
        async with self.transaction(write=True) as repo:
            await self.load(repo, profile_id, data.version)
            await self.configuration(repo, config, writable=True)
            if attempt is not None:
                await attempt.check(repo)
            if signature != await snapshot(repo, self.context, collections):
                conflict(
                    "input_changed",
                    "Input changed during execution; retry against current data.",
                )
            if collections:
                await repo.add(
                    AnalysisResult(
                        id=result_id,
                        run_id=attempt.run_id if attempt is not None else None,
                        tenant_id=self.context.tenant_id,
                        user_id=self.context.user_id,
                        profile_id=profile_id,
                        profile_version=data.version,
                        collection_ids=[str(c) for c in collections],
                        envelope=envelope,
                        snapshot=signature,
                    )
                )
            if attempt is not None:
                await attempt.succeed(repo)
        return envelope

    async def result(self, result_id):
        require_scope(self.context, "results:read")
        require_scope(self.context, "data:read")
        async with self.transaction(write=True) as repo:
            row = found(
                await repo.session.scalar(
                    select(AnalysisResult).where(
                        *owner(AnalysisResult, self.context),
                        AnalysisResult.id == result_id,
                    )
                )
            )
            _, config = await self.load(repo, row.profile_id, row.profile_version)
            await self.configuration(repo, config, writable=True)
            current = await snapshot(
                repo, self.context, [s.collection_id for s in config.selections]
            )
            return {
                **row.envelope,
                "freshness": "current" if row.snapshot == current else "stale",
            }

    async def results(
        self, profile_id, cursor=None, limit=50, version=None, data_state=None
    ):
        require_scope(self.context, "results:read")
        require_scope(self.context, "data:read")
        async with self.transaction(write=True) as repo:
            await self.load(repo, profile_id)
            stmt = select(
                AnalysisResult.id,
                AnalysisResult.profile_version,
                AnalysisResult.snapshot,
                AnalysisResult.envelope["capability"].astext.label("capability"),
                AnalysisResult.envelope["data_state"].astext.label("data_state"),
                AnalysisResult.envelope["generated_at"].astext.label("generated_at"),
            ).where(
                *owner(AnalysisResult, self.context),
                AnalysisResult.profile_id == profile_id,
            )
            if version is not None:
                stmt = stmt.where(AnalysisResult.profile_version == version)
            if data_state is not None:
                stmt = stmt.where(
                    AnalysisResult.envelope["data_state"].astext == data_state
                )
            if cursor:
                stmt = stmt.where(AnalysisResult.id > cursor)
            rows = (
                await repo.session.execute(
                    stmt.order_by(AnalysisResult.id).limit(limit + 1)
                )
            ).all()
            items = []
            for row in rows[:limit]:
                try:
                    _, config = await self.load(repo, profile_id, row.profile_version)
                    await self.configuration(repo, config, writable=True)
                except HTTPException as error:
                    if error.status_code in {403, 404, 410}:
                        continue
                    raise
                current = await snapshot(
                    repo, self.context, [s.collection_id for s in config.selections]
                )
                items.append(
                    {
                        "result_id": str(row.id),
                        "profile_id": str(profile_id),
                        "profile_version": row.profile_version,
                        "capability": row.capability,
                        "execution_status": "succeeded",
                        "data_state": row.data_state,
                        "generated_at": row.generated_at,
                        "freshness": "current" if row.snapshot == current else "stale",
                    }
                )
            return {
                "items": items,
                "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
            }
