import json
import re
from collections.abc import Sequence

from app.db.models import ConversationMessage
from app.query_types import (
    PreparedConversationHistory,
    QueryExecutionError,
)

MAX_HISTORY_EXCHANGES = 5
MAX_HISTORY_CONTEXT_CHARS = 12000

SOURCE_REFERENCE = re.compile(r"\[S[1-9][0-9]*\]")


def prepare_conversation_history(
    messages: Sequence[ConversationMessage],
) -> PreparedConversationHistory:
    selected_messages: list[dict[str, str]] = []
    history_text = "[]"

    for end in range(len(messages), 1, -2):
        if len(selected_messages) >= MAX_HISTORY_EXCHANGES * 2:
            break

        user_message = messages[end - 2]
        assistant_message = messages[end - 1]

        if (
            user_message.role != "user"
            or assistant_message.role != "assistant"
            or user_message.request_id != assistant_message.request_id
            or user_message.session_id != assistant_message.session_id
            or assistant_message.position != user_message.position + 1
        ):
            raise QueryExecutionError(
                "Conversation history contains an invalid message pair."
            )

        pair = [
            {
                "role": "user",
                "content": SOURCE_REFERENCE.sub(
                    "",
                    user_message.content,
                ).strip(),
            },
            {
                "role": "assistant",
                "content": SOURCE_REFERENCE.sub(
                    "",
                    assistant_message.content,
                ).strip(),
            },
        ]

        candidate_messages = [*pair, *selected_messages]

        candidate_text = json.dumps(
            candidate_messages,
            ensure_ascii=False,
        )

        if len(candidate_text) > MAX_HISTORY_CONTEXT_CHARS:
            break

        selected_messages = candidate_messages
        history_text = candidate_text

    return PreparedConversationHistory(
        text=history_text,
        message_count=len(selected_messages),
    )
