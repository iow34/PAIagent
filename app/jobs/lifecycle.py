"""Privacy invalidation shared with ingestion; no provider or scheduler imports."""

from sqlalchemy import func, update

from app.db.models import AnalysisRun
from app.repositories.data_lifecycle import owner


async def erase_runs(session, context, collection_ids):
    for cid in set(collection_ids):
        await session.execute(
            update(AnalysisRun)
            .where(
                *owner(AnalysisRun, context),
                AnalysisRun.collection_ids.contains([str(cid)]),
            )
            .values(
                status="cancelled",
                finished_at=func.clock_timestamp(),
                input={},
                fingerprint="",
                error_code="data_erased",
                lease_token=None,
                lease_expires_at=None,
            )
        )
