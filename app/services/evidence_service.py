"""Shared context access, current-evidence hybrid search and file attachment."""

import hashlib

from litestar.exceptions import HTTPException, NotFoundException
from sqlalchemy import delete, func, or_, select

from app.db.models import DataDelivery, DataObject, EvidenceFragment, FileProvenance
from app.integration.validation import ContractError
from app.normalization.common import fragments, parse_file
from app.repositories.data_lifecycle import owner
from app.security.permissions import require_scope, require_source
from app.services.data_service import DataService


class EvidenceService(DataService):
    def statement(self):
        return (
            select(EvidenceFragment, DataDelivery)
            .join(DataDelivery, DataDelivery.id == EvidenceFragment.delivery_id)
            .outerjoin(DataObject, DataObject.id == DataDelivery.object_id)
            .where(
                *owner(DataDelivery, self.context),
                DataDelivery.erased.is_(False),
                DataDelivery.corrected_by_id.is_(None),
                or_(
                    DataDelivery.object_id.is_(None),
                    (DataObject.deleted.is_(False))
                    & (DataObject.object_version == DataDelivery.object_version),
                ),
            )
        )

    def view(self, fragment, delivery):
        return {
            "id": str(fragment.id),
            "delivery_id": str(delivery.id),
            "object_id": str(delivery.object_id) if delivery.object_id else None,
            "object_version": delivery.object_version,
            "schema_version": delivery.schema_version,
            "source_id": str(delivery.source_id),
            "collection_id": str(delivery.collection_id),
            "event_id": delivery.event_id,
            "ordinal": fragment.ordinal,
            "content": fragment.content,
            "position": fragment.position,
        }

    async def attach(self, collection_id, data, raw, extension):
        require_scope(self.context, "data:write")
        if data.operation not in {"snapshot", "upsert"}:
            raise ContractError("Files require a full-state record envelope.")
        async with self.transaction() as repo:
            await self.collection(repo, collection_id, writable=True)
        extracted = await parse_file(raw, extension)
        values = fragments(extracted.units)
        digest = hashlib.sha256(raw).hexdigest()
        async with self.transaction(write=True) as repo:
            ack = await self._ingest(repo, collection_id, data, "records")
            existing = await repo.session.get(FileProvenance, ack.delivery_id)
            if existing:
                if existing.digest != digest:
                    raise HTTPException(
                        status_code=409,
                        detail="File version reused with different bytes.",
                    )
            elif ack.status == "duplicate":
                raise HTTPException(
                    status_code=409,
                    detail="Existing JSON-only version cannot acquire a file; use a higher object_version.",
                )
            else:
                await repo.session.execute(
                    delete(EvidenceFragment).where(
                        EvidenceFragment.delivery_id == ack.delivery_id
                    )
                )
                for ordinal, (content, position) in enumerate(values):
                    await repo.add(
                        EvidenceFragment(
                            delivery_id=ack.delivery_id,
                            ordinal=ordinal,
                            content=content,
                            position=position,
                        )
                    )
                await repo.add(
                    FileProvenance(
                        delivery_id=ack.delivery_id,
                        digest=digest,
                        format=extension,
                        coverage=extracted.coverage,
                        total_parts=extracted.total_parts,
                        limitations=extracted.limitations,
                    )
                )
        return {
            "ack": ack,
            "extraction_coverage": extracted.coverage,
            "total_parts": extracted.total_parts,
            "fragments": len(values),
            "limitations": extracted.limitations,
            "indexed": False,
        }

    async def provenance(self, repo, delivery_id):
        file = await repo.session.get(FileProvenance, delivery_id)
        if file is None:
            return {
                "format": "json",
                "extraction_coverage": "full",
                "limitations": ["Search text uses selected schema fields only."],
            }
        return {
            "format": file.format,
            "extraction_coverage": file.coverage,
            "total_parts": file.total_parts,
            "limitations": file.limitations,
        }

    async def context_page(self, collection_id, cursor=None, limit=50):
        require_scope(self.context, "data:read")
        async with self.transaction() as repo:
            await self.collection(repo, collection_id)
            statement = self.statement().where(
                DataDelivery.collection_id == collection_id
            )
            if cursor:
                statement = statement.where(EvidenceFragment.id > cursor)
            rows = (
                await repo.session.execute(
                    statement.order_by(EvidenceFragment.id).limit(limit + 1)
                )
            ).all()
            return {
                "items": [
                    {**self.view(f, d), "extraction": await self.provenance(repo, d.id)}
                    for f, d in rows[:limit]
                ],
                "next_cursor": str(rows[limit - 1][0].id)
                if len(rows) > limit
                else None,
                "selection_complete": len(rows) <= limit,
                "collection_coverage": "unknown",
                "ordering": "UUID; restart enumeration after concurrent changes",
            }

    async def get_evidence(self, fragment_id):
        require_scope(self.context, "data:read")
        async with self.transaction() as repo:
            row = (
                await repo.session.execute(
                    self.statement().where(EvidenceFragment.id == fragment_id)
                )
            ).first()
            if row is None:
                raise NotFoundException(detail="Evidence unavailable or superseded.")
            f, d = row
            require_source(self.context, d.source_id)
            return {**self.view(f, d), "extraction": await self.provenance(repo, d.id)}

    async def index(self, collection_id, embedding_service, limit=64):
        require_scope(self.context, "data:write")
        async with self.transaction() as repo:
            await self.collection(repo, collection_id, writable=True)
            rows = (
                await repo.session.execute(
                    self.statement()
                    .where(
                        DataDelivery.collection_id == collection_id,
                        EvidenceFragment.embedding.is_(None),
                    )
                    .order_by(EvidenceFragment.id)
                    .limit(limit)
                )
            ).all()
            inputs = [(f.id, f.content) for f, d in rows]
        if not inputs:
            return {"indexed": 0}
        result = await embedding_service.embed_texts([content for _, content in inputs])
        async with self.transaction(write=True) as repo:
            await self.collection(repo, collection_id, writable=True)
            rows = (
                await repo.session.execute(
                    self.statement().where(
                        EvidenceFragment.id.in_([i for i, _ in inputs])
                    )
                )
            ).all()
            current = {f.id: f for f, d in rows}
            for (identifier, content), vector in zip(
                inputs, result.vectors, strict=True
            ):
                f = current.get(identifier)
                if f is None or f.content != content:
                    raise HTTPException(
                        status_code=409, detail="Evidence changed during indexing."
                    )
                f.embedding = vector
                f.embedding_model = result.model
        return {
            "indexed": len(inputs),
            "continuation": "Repeat until indexed=0; no collection completeness is inferred.",
        }

    async def search(self, collection_id, query, embedding_service, limit=5):
        require_scope(self.context, "data:read")
        if not query.strip() or len(query) > 2000 or "\x00" in query:
            raise ContractError("Invalid search query.")
        async with self.transaction() as repo:
            await self.collection(repo, collection_id)
        result = await embedding_service.embed_texts([query])
        async with self.transaction() as repo:
            await self.collection(repo, collection_id)
            base = self.statement().where(DataDelivery.collection_id == collection_id)
            vector = (
                base.where(
                    EvidenceFragment.embedding_model == result.model,
                    EvidenceFragment.embedding.is_not(None),
                )
                .order_by(
                    EvidenceFragment.embedding.cosine_distance(result.vectors[0]),
                    EvidenceFragment.id,
                )
                .limit(40)
            )
            document = func.to_tsvector("simple", EvidenceFragment.content)
            tsquery = func.plainto_tsquery("simple", query)
            lexical = (
                base.where(document.op("@@")(tsquery))
                .order_by(
                    func.ts_rank_cd(document, tsquery).desc(), EvidenceFragment.id
                )
                .limit(40)
            )
            scores = {}
            hits = {}
            for statement in (vector, lexical):
                rows = (await repo.session.execute(statement)).all()
                for rank, (f, d) in enumerate(rows, 1):
                    scores[f.id] = scores.get(f.id, 0) + 1 / (60 + rank)
                    hits[f.id] = self.view(f, d)
            return {
                "items": [
                    {**hits[i], "score": scores[i]}
                    for i in sorted(scores, key=lambda i: (-scores[i], str(i)))[:limit]
                ],
                "coverage": "partial",
                "limitations": [
                    "Top-ranked sample; missing embeddings are excluded from vector ranking. simple full-text is not Ukrainian morphology."
                ],
            }

    async def normalize_existing(self, collection_id, cursor=None, limit=50):
        from app.normalization.common import structured
        from app.repositories.integration import IntegrationRepository

        require_scope(self.context, "data:write")
        async with self.transaction(write=True) as repo:
            await self.collection(repo, collection_id, writable=True)
            statement = (
                select(DataDelivery)
                .outerjoin(DataObject, DataObject.id == DataDelivery.object_id)
                .where(
                    *owner(DataDelivery, self.context),
                    DataDelivery.collection_id == collection_id,
                    DataDelivery.erased.is_(False),
                    DataDelivery.corrected_by_id.is_(None),
                    or_(
                        DataDelivery.object_id.is_(None),
                        (DataObject.deleted.is_(False))
                        & (DataObject.object_version == DataDelivery.object_version),
                    ),
                )
            )
            if cursor:
                statement = statement.where(DataDelivery.id > cursor)
            deliveries = list(
                await repo.session.scalars(
                    statement.order_by(DataDelivery.id).limit(limit + 1)
                )
            )
            normalized = 0
            for delivery in deliveries[:limit]:
                if await repo.session.get(FileProvenance, delivery.id):
                    continue
                exists = await repo.session.scalar(
                    select(EvidenceFragment.id)
                    .where(EvidenceFragment.delivery_id == delivery.id)
                    .limit(1)
                )
                if exists or delivery.payload is None:
                    continue
                schema = await IntegrationRepository(repo.session, self.context).schema(
                    collection_id,
                    "event" if delivery.operation == "observe" else "record",
                    delivery.schema_version,
                )
                if schema is None:
                    raise ContractError("Original schema unavailable.")
                for ordinal, (content, position) in enumerate(
                    structured(delivery.payload, schema.field_mappings)
                ):
                    await repo.add(
                        EvidenceFragment(
                            delivery_id=delivery.id,
                            ordinal=ordinal,
                            content=content,
                            position=position,
                        )
                    )
                normalized += 1
            return {
                "normalized": normalized,
                "next_cursor": str(deliveries[limit - 1].id)
                if len(deliveries) > limit
                else None,
                "coverage": "Current non-erased evidence only; repeat traversal after concurrent writes.",
            }
