"""Separate worker process; PostgreSQL owns all accepted work and scheduling state."""

import argparse
import asyncio
import logging
from uuid import UUID

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError

from app.capabilities.generation import OpenAIGenerator
from app.config.settings import get_settings
from app.db.models import AnalysisRun
from app.db.postgres import open_database
from app.security.context import AccessContext
from app.services.embedding_service import EmbeddingService
from app.services.job_service import JobService
from app.services.trigger_service import schedule

logger = logging.getLogger(__name__)


async def serve(
    factory, generator, embeddings, *, concurrency=4, poll_seconds=1.0, once=False
):
    service = JobService(factory, AccessContext(UUID(int=0), "worker", ()))
    active = set()
    try:
        while True:
            for task in list(active):
                if task.done():
                    active.remove(task)
                    try:
                        task.result()
                    except Exception as error:  # noqa: BLE001 - process boundary, private details omitted
                        logger.error(
                            "Worker attempt failed (%s).", type(error).__name__
                        )
            try:
                async with asyncio.timeout(10):
                    await schedule(factory)
                    while len(active) < concurrency:
                        claim = await service.claim()
                        if claim is None:
                            break
                        active.add(
                            asyncio.create_task(
                                service.execute(claim, generator, embeddings)
                            )
                        )
            except Exception as error:
                # Never log exception repr/SQL: parameters can contain private input.
                logger.error(
                    "Worker control plane unavailable (%s).", type(error).__name__
                )
                if once:
                    raise
            if once and not active:
                return
            await asyncio.sleep(poll_seconds)
    finally:
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)


async def main_async(args):
    settings = get_settings()
    async with (
        open_database(settings.database_url) as database,
        AsyncOpenAI(
            api_key=settings.openai_api_key,
            timeout=settings.openai_timeout_seconds,
            max_retries=0,
        ) as client,
    ):
        try:
            async with database.session_factory() as session:
                await session.execute(select(AnalysisRun).limit(0))
        except ProgrammingError:
            raise RuntimeError(
                "Worker schema is unavailable; apply alembic upgrade head first."
            ) from None
        await serve(
            database.session_factory,
            OpenAIGenerator(client, settings.generation_model),
            EmbeddingService(client, settings),
            concurrency=args.concurrency,
            poll_seconds=args.poll_seconds,
            once=args.once,
        )


def main():
    parser = argparse.ArgumentParser(
        description="PAIagent PostgreSQL analysis worker and scheduler"
    )
    parser.add_argument("--concurrency", type=int, choices=range(1, 17), default=4)
    parser.add_argument("--poll-seconds", type=float, default=1)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Drain currently due work; delayed retries/future schedules remain durable.",
    )
    args = parser.parse_args()
    if not 0.1 <= args.poll_seconds <= 30:
        parser.error("--poll-seconds must be between 0.1 and 30")
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
