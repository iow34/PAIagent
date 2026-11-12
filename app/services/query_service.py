import asyncio
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.query_types import QueryResult, QueryWorkflow
from app.repositories.conversations import ConversationRepository
from app.security.context import AccessContext
from app.services.conversation_history import (
    MAX_HISTORY_EXCHANGES,
    prepare_conversation_history,
)
from app.services.search_service import (
    MAX_SEARCH_QUERY_CHARS,
    SearchInputError,
)

QUERY_TIMEOUT_SECONDS = 60.0


class QueryInputError(ValueError):
    pass


class QueryService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        workflow: QueryWorkflow,
    ) -> None:
        self._session_factory = session_factory
        self._workflow = workflow

    async def query(
        self,
        *,
        context: AccessContext,
        question: str,
        session_id: UUID | None = None,
    ) -> QueryResult:
        if len(question) > MAX_SEARCH_QUERY_CHARS:
            raise QueryInputError(
                f"Question must not exceed {MAX_SEARCH_QUERY_CHARS} characters."
            )

        question = question.strip()

        if not question:
            raise QueryInputError("Question must not be empty.")

        if "\x00" in question:
            raise QueryInputError("Question must not contain NUL characters.")

        request_id = uuid4()

        async with asyncio.timeout(QUERY_TIMEOUT_SECONDS):
            history = prepare_conversation_history([])
            expected_last_position = -1

            if session_id is not None:
                async with self._session_factory.begin() as db_session:
                    repository = ConversationRepository(db_session)

                    messages = await repository.list_recent_messages(
                        context=context,
                        session_id=session_id,
                        limit=MAX_HISTORY_EXCHANGES * 2,
                    )

                    history = prepare_conversation_history(messages)
                    if messages:
                        expected_last_position = messages[-1].position

            try:
                answer = await self._workflow.run(
                    context=context,
                    question=question,
                    history=history,
                )
            except SearchInputError as error:
                raise QueryInputError(str(error)) from error

            async with self._session_factory.begin() as db_session:
                repository = ConversationRepository(db_session)

                if session_id is None:
                    conversation = await repository.create_session(
                        context=context,
                    )
                    resolved_session_id = conversation.id
                else:
                    resolved_session_id = session_id

                await repository.add_exchange(
                    context=context,
                    session_id=resolved_session_id,
                    request_id=request_id,
                    question=question,
                    answer=answer,
                    expected_last_position=expected_last_position,
                )

            return QueryResult(
                session_id=resolved_session_id,
                request_id=request_id,
                answer=answer.answer,
                sources=answer.sources,
                status=answer.status,
            )
