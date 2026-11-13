"""Owner-scoped snapshots and scalar selection without generated SQL."""

import json
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String, and_, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by

from app.capabilities.validation import base_type, node_at, value_at
from app.db.models import (
    DataDelivery,
    DataObject,
    DataOutbox,
    EvidenceFragment,
    FileProvenance,
)
from app.integration.validation import ContractError
from app.repositories.data_lifecycle import owner
from app.services.data_service import digest


def scalar_expr(payload, path):
    expression = payload
    for part in path[1:].split("/"):
        expression = expression[part]
    return expression.astext


def statement(context, selection, schema, config):
    d = DataDelivery
    stmt = (
        select(d)
        .outerjoin(DataObject, DataObject.id == d.object_id)
        .where(
            *owner(d, context),
            d.collection_id == selection.collection_id,
            d.schema_version == selection.schema_version,
            d.erased.is_(False),
            d.corrected_by_id.is_(None),
            d.payload.is_not(None),
            d.operation == "observe"
            if selection.kind == "event"
            else d.operation.in_(["snapshot", "upsert"]),
        )
    )
    if selection.mode == "current":
        stmt = stmt.where(
            DataObject.deleted.is_(False), DataObject.object_version == d.object_version
        )
    for f in selection.filters:
        expression = scalar_expr(d.payload, f.field)
        kind = base_type(node_at(schema, f.field))
        if kind in {"integer", "number"}:
            expression = cast(expression, Numeric)

        def typed(v, kind=kind):
            if kind in {"integer", "number"}:
                return Decimal(str(v))
            if kind == "boolean":
                return "true" if v else "false"
            return v

        if f.op == "is_null":
            clause = expression.is_(None)
        elif f.op == "not_null":
            clause = expression.is_not(None)
        elif f.op == "in":
            clause = expression.in_([typed(v) for v in f.value])
        else:
            value = typed(f.value)
            clause = {
                "eq": expression.__eq__,
                "ne": expression.__ne__,
                "gt": expression.__gt__,
                "gte": expression.__ge__,
                "lt": expression.__lt__,
                "lte": expression.__le__,
            }[f.op](value)
        stmt = stmt.where(clause)
    time = (
        d.occurred_at
        if selection.time_field == "occurred_at"
        else cast(scalar_expr(d.payload, selection.time_field), DateTime(timezone=True))
    )
    if config.period:
        periods = [and_(time >= config.period.start, time < config.period.end)]
        if config.comparison_period:
            periods.append(
                and_(
                    time >= config.comparison_period.start,
                    time < config.comparison_period.end,
                )
            )
        stmt = stmt.where(or_(*periods))
    return stmt


async def snapshot(repo, context, collection_ids):
    # Includes changes outside a truncated selection and insertions into empty data.
    # Counting and max(timestamp) alone can alias after erasure: PostgreSQL now()
    # is transaction-start time, not lock-acquisition/commit order. Hash the full
    # ordered set of opaque mutation IDs so a replacement always changes it.
    ordered_ids = func.string_agg(
        cast(DataOutbox.id, String), aggregate_order_by(literal(""), DataOutbox.id)
    )
    fingerprint = func.encode(func.sha256(func.convert_to(ordered_ids, "UTF8")), "hex")
    values = (
        await repo.session.execute(
            select(
                DataOutbox.collection_id,
                fingerprint,
            )
            .where(
                *owner(DataOutbox, context),
                DataOutbox.collection_id.in_(collection_ids),
            )
            .group_by(DataOutbox.collection_id)
            .order_by(DataOutbox.collection_id)
        )
    ).all()
    return digest([[str(cid), signature] for cid, signature in values])


async def collect(repo, context, config, schemas):
    samples, sources, coverage = [], [], []
    remaining = config.budget.max_records
    fragment_remaining = config.budget.max_fragments
    for index, selection in enumerate(config.selections):
        stmt = statement(context, selection, schemas[index].json_schema, config)
        total = await repo.session.scalar(
            select(func.count()).select_from(stmt.subquery())
        )
        if config.metrics and total > remaining:
            raise ContractError(
                "Exact metric input exceeds max_records; narrow filters or increase budget.",
                code="exact_input_limit",
            )
        rows = (
            list(
                await repo.session.scalars(
                    stmt.order_by(DataDelivery.id).limit(remaining)
                )
            )
            if remaining
            else []
        )
        remaining -= len(rows)
        record_samples = []
        ids = [r.id for r in rows]
        fragment_rows = []
        if selection.include_text and ids and fragment_remaining:
            fragment_rows = (
                await repo.session.execute(
                    select(EvidenceFragment, FileProvenance)
                    .join(
                        FileProvenance,
                        FileProvenance.delivery_id == EvidenceFragment.delivery_id,
                    )
                    .where(EvidenceFragment.delivery_id.in_(ids))
                    .order_by(EvidenceFragment.delivery_id, EvidenceFragment.ordinal)
                    .limit(fragment_remaining)
                )
            ).all()
        fragment_count = (
            await repo.session.scalar(
                select(func.count())
                .select_from(EvidenceFragment)
                .join(
                    FileProvenance,
                    FileProvenance.delivery_id == EvidenceFragment.delivery_id,
                )
                .where(EvidenceFragment.delivery_id.in_(ids))
            )
            if selection.include_text and ids
            else 0
        )
        fragment_remaining -= len(fragment_rows)
        for row in rows:
            source_id = str(row.id)
            record_samples.append(
                {
                    "id": source_id,
                    "payload": row.payload,
                    "occurred_at": row.occurred_at,
                }
            )
            selected = {path: value_at(row.payload, path) for path in selection.fields}
            sources.append(
                {
                    "id": source_id,
                    "delivery_id": source_id,
                    "evidence_path": f"/v1/events/{source_id}",
                    "source_id": str(row.source_id),
                    "collection_id": str(row.collection_id),
                    "object_id": str(row.object_id) if row.object_id else None,
                    "object_version": row.object_version,
                    "schema_version": row.schema_version,
                    "event_id": row.event_id,
                    "occurred_at": row.occurred_at.isoformat(),
                    "content": json.dumps(
                        selected, ensure_ascii=False, allow_nan=False
                    ),
                    "position": {"fields": selection.fields},
                    "selection": index,
                }
            )
        for fragment, file in fragment_rows:
            # JSON selected fields are already projected above. include_text explicitly
            # permits document file contents; never leak other JSON search fields.
            if file is None:
                continue
            parent = next(
                s for s in reversed(sources) if s["id"] == str(fragment.delivery_id)
            )
            sources.append(
                {
                    **parent,
                    "id": str(fragment.id),
                    "content": fragment.content,
                    "position": fragment.position,
                    "extraction_coverage": file.coverage,
                    "extraction_limitations": file.limitations,
                }
            )
        samples.append(record_samples)
        coverage.append(
            {
                "selection": index,
                "collection_id": str(selection.collection_id),
                "schema_version": selection.schema_version,
                "kind": selection.kind,
                "mode": selection.mode,
                "matched_rows": total,
                "selected_rows": len(rows),
                "matched_fragments": fragment_count,
                "selected_fragments": len(fragment_rows),
                "complete_selection": total == len(rows)
                and fragment_count == len(fragment_rows),
                "collection_completeness": "unknown",
                "history_complete": False,
            }
        )
    return samples, sources, coverage
