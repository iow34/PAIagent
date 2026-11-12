"""Deadline around dependency resolution and the complete step-5 handler."""

import asyncio
import json

from litestar.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_TIMEOUT_SECONDS = 60.0


class ExecutionTimeoutMiddleware:
    def __init__(self, app: ASGIApp, *, error_code: str) -> None:
        self.app = app
        self.error_code = error_code

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                await self.app(scope, receive, tracked_send)
        except TimeoutError:
            if started:
                # A second response cannot replace headers already sent.
                raise
            body = json.dumps(
                {
                    "status_code": 504,
                    "detail": "Request execution timed out.",
                    "extra": {"code": self.error_code},
                }
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 504,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
