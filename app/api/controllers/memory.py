"""Explicit opt-in memory, source candidates and result feedback."""

from typing import Any, Literal
from uuid import UUID

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.di import NamedDependency, Provide
from litestar.middleware import DefineMiddleware
from litestar.params import JSONBody
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.controllers.integration import (
    PageLimit,
    check_query_parameters,
    contract_error_handler,
)
from app.api.execution_timeout import ExecutionTimeoutMiddleware
from app.integration.validation import ContractError
from app.personalization.types import (
    ExtractionInput,
    ExtractionView,
    FeedbackInput,
    FeedbackPage,
    FeedbackView,
    MemoryAction,
    MemoryPage,
    MemoryView,
    MessageCandidates,
    MessageInput,
    MessageUpdate,
    MessageView,
    PreferenceWrite,
    SettingsInput,
    SettingsView,
)
from app.security.authentication import get_access_context
from app.security.context import AccessContext
from app.services.feedback_service import FeedbackService


def memory_service(
    db_session_factory: NamedDependency[async_sessionmaker[AsyncSession]],
    access_context: NamedDependency[AccessContext],
) -> FeedbackService:
    return FeedbackService(db_session_factory, access_context)


class MemoryController(Controller):
    path = "/v1"
    tags = ["Managed memory and feedback"]  # noqa: RUF012
    security = [{"ServiceAPIKey": []}]  # noqa: RUF012
    request_max_body_size = 98304
    before_request = check_query_parameters
    dependencies = {  # noqa: RUF012 - Litestar configuration
        "access_context": Provide(get_access_context),
        "memory_service": Provide(memory_service, sync_to_thread=False),
    }
    exception_handlers = {ContractError: contract_error_handler}  # noqa: RUF012
    middleware = [  # noqa: RUF012 - Litestar configuration
        DefineMiddleware(ExecutionTimeoutMiddleware, error_code="memory_timeout")
    ]

    @get("/memory/settings", opt={"required_scope": "memory:read"})
    async def settings(
        self, memory_service: NamedDependency[FeedbackService]
    ) -> SettingsView:
        return msgspec.convert(await memory_service.get_settings(), type=SettingsView)

    @put("/memory/settings", opt={"required_scope": "memory:write"})
    async def configure(
        self,
        data: JSONBody[SettingsInput],
        memory_service: NamedDependency[FeedbackService],
    ) -> SettingsView:
        return msgspec.convert(await memory_service.configure(data), type=SettingsView)

    @delete("/memory", status_code=200, opt={"required_scope": "memory:write"})
    async def forget(
        self, memory_service: NamedDependency[FeedbackService]
    ) -> dict[str, Any]:
        return await memory_service.forget_all()

    @put("/memory/preferences", opt={"required_scope": "memory:write"})
    async def preference(
        self,
        data: JSONBody[PreferenceWrite],
        memory_service: NamedDependency[FeedbackService],
    ) -> MemoryView:
        return msgspec.convert(
            await memory_service.put_preference(data), type=MemoryView
        )

    @get(
        "/memory/items",
        opt={
            "required_scope": "memory:read",
            "query_parameters": ("cursor", "limit", "collection_id", "status"),
        },
    )
    async def items(
        self,
        memory_service: NamedDependency[FeedbackService],
        cursor: UUID | None = None,
        limit: PageLimit = 50,
        collection_id: UUID | None = None,
        status: Literal[
            "candidate", "confirmed", "superseded", "invalidated", "deleted", "rejected"
        ]
        | None = None,
    ) -> MemoryPage:
        return msgspec.convert(
            await memory_service.items(cursor, limit, collection_id, status),
            type=MemoryPage,
        )

    @get("/memory/items/{item_id:uuid}", opt={"required_scope": "memory:read"})
    async def item(
        self, item_id: UUID, memory_service: NamedDependency[FeedbackService]
    ) -> MemoryView:
        return msgspec.convert(await memory_service.get_item(item_id), type=MemoryView)

    @post(
        "/memory/items/{item_id:uuid}/confirm",
        status_code=200,
        opt={"required_scope": "memory:write"},
    )
    async def confirm(
        self,
        item_id: UUID,
        data: JSONBody[MemoryAction],
        memory_service: NamedDependency[FeedbackService],
    ) -> MemoryView:
        return msgspec.convert(
            await memory_service.confirm(item_id, data), type=MemoryView
        )

    @delete(
        "/memory/items/{item_id:uuid}",
        status_code=200,
        opt={"required_scope": "memory:write"},
    )
    async def remove(
        self, item_id: UUID, memory_service: NamedDependency[FeedbackService]
    ) -> MemoryView:
        return msgspec.convert(
            await memory_service.delete_item(item_id), type=MemoryView
        )

    @post("/memory/messages", status_code=200, opt={"required_scope": "memory:write"})
    async def message(
        self,
        data: JSONBody[MessageInput],
        memory_service: NamedDependency[FeedbackService],
    ) -> MessageCandidates:
        return msgspec.convert(
            await memory_service.message(data), type=MessageCandidates
        )

    @get("/memory/messages/{message_id:uuid}", opt={"required_scope": "memory:read"})
    async def get_message(
        self, message_id: UUID, memory_service: NamedDependency[FeedbackService]
    ) -> MessageView:
        return msgspec.convert(
            await memory_service.get_message(message_id), type=MessageView
        )

    @put("/memory/messages/{message_id:uuid}", opt={"required_scope": "memory:write"})
    async def edit_message(
        self,
        message_id: UUID,
        data: JSONBody[MessageUpdate],
        memory_service: NamedDependency[FeedbackService],
    ) -> MessageCandidates:
        return msgspec.convert(
            await memory_service.edit_message(message_id, data), type=MessageCandidates
        )

    @delete(
        "/memory/messages/{message_id:uuid}",
        status_code=200,
        opt={"required_scope": "memory:write"},
    )
    async def delete_message(
        self, message_id: UUID, memory_service: NamedDependency[FeedbackService]
    ) -> dict[str, Any]:
        return await memory_service.delete_message(message_id)

    @post(
        "/memory/extractions", status_code=200, opt={"required_scope": "memory:write"}
    )
    async def extract(
        self,
        data: JSONBody[ExtractionInput],
        memory_service: NamedDependency[FeedbackService],
    ) -> ExtractionView:
        return msgspec.convert(await memory_service.extract(data), type=ExtractionView)

    @post(
        "/results/{result_id:uuid}/feedback",
        status_code=200,
        opt={"required_scope": "feedback:write"},
    )
    async def feedback(
        self,
        result_id: UUID,
        data: JSONBody[FeedbackInput],
        memory_service: NamedDependency[FeedbackService],
    ) -> FeedbackView:
        return msgspec.convert(
            await memory_service.submit(result_id, data), type=FeedbackView
        )

    @get(
        "/results/{result_id:uuid}/feedback",
        opt={
            "required_scope": "feedback:read",
            "query_parameters": ("cursor", "limit"),
        },
    )
    async def feedback_list(
        self,
        result_id: UUID,
        memory_service: NamedDependency[FeedbackService],
        cursor: UUID | None = None,
        limit: PageLimit = 50,
    ) -> FeedbackPage:
        return msgspec.convert(
            await memory_service.feedback(result_id, cursor, limit), type=FeedbackPage
        )

    @delete(
        "/results/{result_id:uuid}/feedback",
        status_code=200,
        opt={"required_scope": "feedback:write"},
    )
    async def forget_feedback(
        self, result_id: UUID, memory_service: NamedDependency[FeedbackService]
    ) -> dict[str, Any]:
        return await memory_service.delete_feedback(result_id)

    @get("/feedback/profiles", opt={"required_scope": "feedback:read"})
    async def scores(
        self, memory_service: NamedDependency[FeedbackService]
    ) -> dict[str, Any]:
        return await memory_service.profile_scores()
