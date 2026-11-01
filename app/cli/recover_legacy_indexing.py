import argparse
import asyncio
import os
from uuid import UUID

from dotenv import load_dotenv

from app.db.postgres import open_database
from app.repositories.documents import DocumentRepository
from app.security.context import AccessContext


async def run(
    *,
    context: AccessContext,
    document_id: UUID,
    apply: bool,
) -> None:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL is required.")

    async with open_database(database_url) as database:
        async with database.session_factory.begin() as session:
            await DocumentRepository(session).recover_legacy_processing(
                context=context,
                document_id=document_id,
                apply=apply,
            )

    if apply:
        print(f"{document_id}: marked failed; retry via the reindex endpoint.")
    else:
        print(f"{document_id}: eligible for legacy recovery; no changes made.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recover one legacy processing document after stopping all workers."
    )
    parser.add_argument("--tenant-id", type=UUID, required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--document-id", type=UUID, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--workers-stopped", action="store_true")
    args = parser.parse_args()

    if args.apply and not args.workers_stopped:
        parser.error("--apply requires --workers-stopped.")

    user_id = args.user_id
    if (
        not 1 <= len(user_id) <= 255
        or user_id != user_id.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in user_id)
    ):
        parser.error("--user-id must be valid and have no surrounding whitespace.")

    try:
        asyncio.run(
            run(
                context=AccessContext(args.tenant_id, user_id),
                document_id=args.document_id,
                apply=args.apply,
            )
        )
    except (LookupError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
