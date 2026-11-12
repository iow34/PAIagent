from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.answer_types import AnswerResult
from app.db.models import ConversationMessage, ConversationSession
from app.security.context import AccessContext

MAX_HISTORY_MESSAGES = 50


class ConversationNotFoundError(LookupError):
    pass


class ConversationRequestConflictError(ValueError):
    pass


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_session(
        self,
        *,
        context: AccessContext,
    ) -> ConversationSession:
        conversation = ConversationSession(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )

        self._session.add(conversation)
        await self._session.flush()

        return conversation

    async def get_session(
        self,
        *,
        context: AccessContext,
        session_id: UUID,
        for_update: bool = False,
    ) -> ConversationSession:
        statement = select(ConversationSession).where(
            ConversationSession.id == session_id,
            ConversationSession.tenant_id == context.tenant_id,
            ConversationSession.user_id == context.user_id,
        )

        if for_update:
            statement = statement.with_for_update()

        conversation = await self._session.scalar(statement)

        if conversation is None:
            raise ConversationNotFoundError("Conversation is unavailable.")

        return conversation

    async def add_exchange(
        self,
        *,
        context: AccessContext,
        session_id: UUID,
        request_id: UUID,
        question: str,
        answer: AnswerResult,
        expected_last_position: int | None = None,
    ) -> tuple[ConversationMessage, ConversationMessage]:
        conversation = await self.get_session(
            context=context,
            session_id=session_id,
            for_update=True,
        )

        existing_message_id = await self._session.scalar(
            select(ConversationMessage.id)
            .where(
                ConversationMessage.session_id == conversation.id,
                ConversationMessage.request_id == request_id,
            )
            .limit(1)
        )

        if existing_message_id is not None:
            raise ConversationRequestConflictError(
                "This request already has messages in the conversation."
            )

        last_position: int | None = await self._session.scalar(
            select(func.max(ConversationMessage.position)).where(
                ConversationMessage.session_id == conversation.id,
            )
        )

        if (
            expected_last_position is not None
            and (-1 if last_position is None else last_position)
            != expected_last_position
        ):
            raise ConversationRequestConflictError(
                "Conversation changed while the answer was being generated."
            )

        if last_position is None:
            next_position = 0
        else:
            next_position = last_position + 1

        serialized_sources: list[dict[str, object]] = [
            {
                "source_id": source.source_id,
                "chunk_id": str(source.chunk_id),
                "document_id": str(source.document_id),
                "chunk_index": source.chunk_index,
                "content": source.content,
            }
            for source in answer.sources
        ]

        user_message = ConversationMessage(
            session_id=conversation.id,
            request_id=request_id,
            position=next_position,
            role="user",
            content=question,
            answer_status=None,
            sources=[],
        )

        assistant_message = ConversationMessage(
            session_id=conversation.id,
            request_id=request_id,
            position=next_position + 1,
            role="assistant",
            content=answer.answer,
            answer_status=answer.status,
            sources=serialized_sources,
        )

        self._session.add_all([user_message, assistant_message])

        await self._session.flush()

        return user_message, assistant_message

    async def list_recent_messages(
        self,
        *,
        context: AccessContext,
        session_id: UUID,
        limit: int = 20,
    ) -> list[ConversationMessage]:
        if not 1 <= limit <= MAX_HISTORY_MESSAGES:
            raise ValueError(
                f"History limit must be between 1 and {MAX_HISTORY_MESSAGES}."
            )

        await self.get_session(
            context=context,
            session_id=session_id,
        )

        statement = (
            select(ConversationMessage)
            .join(
                ConversationSession,
                ConversationMessage.session_id == ConversationSession.id,
            )
            .where(
                ConversationSession.id == session_id,
                ConversationSession.tenant_id == context.tenant_id,
                ConversationSession.user_id == context.user_id,
            )
            .order_by(ConversationMessage.position.desc())
            .limit(limit)
        )

        result = await self._session.scalars(statement)
        messages = list(result)
        messages.reverse()

        return messages
