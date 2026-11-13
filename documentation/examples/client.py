"""Standalone HTTP helpers. Requires httpx; imports no PAIagent server code."""

import math
import time
from collections.abc import Iterator
from typing import Any

import httpx


class Client:
    def __init__(self, url: str, key: str, user: str, *, timeout: float = 120):
        self.timeout = timeout
        self.http = httpx.Client(
            base_url=url,
            headers={"Authorization": "Bearer " + key, "X-User-ID": user},
            timeout=30,
        )

    def close(self) -> None:
        self.http.close()

    def request(
        self, method: str, path: str, *, replay_safe: bool = False, **kwargs: Any
    ) -> Any:
        """Retry only reads or explicitly replay-safe delivery, within a deadline."""
        deadline = time.monotonic() + self.timeout
        for attempt in range(4):
            try:
                response = self.http.request(method, path, **kwargs)
            except httpx.TransportError:
                if not replay_safe or attempt == 3 or time.monotonic() >= deadline:
                    raise
                delay = 2**attempt
            else:
                if (
                    response.status_code not in {429, 502, 503, 504}
                    or not replay_safe
                    or attempt == 3
                ):
                    response.raise_for_status()
                    return response.json()
                try:
                    delay = max(
                        float(
                            response.headers.get(
                                "Retry-After",
                                response.headers.get(
                                    "RateLimit-Reset", str(2**attempt)
                                ),
                            )
                        ),
                        0.1,
                    )
                    if not math.isfinite(delay):
                        raise ValueError("Non-finite retry delay")
                except ValueError:
                    # HTTP-date Retry-After is deliberately returned to the caller.
                    response.raise_for_status()
                    raise
            if time.monotonic() + delay >= deadline:
                raise TimeoutError(
                    "Retry deadline exceeded; preserve the original request identity."
                )
            time.sleep(delay)
        raise RuntimeError("Unreachable")

    def pages(self, path: str, **params: Any) -> Iterator[dict[str, Any]]:
        seen = set()
        while True:
            page = self.request("GET", path, replay_safe=True, params=params)
            yield from page["items"]
            cursor = page["next_cursor"]
            if cursor is None:
                return
            if cursor in seen:
                raise RuntimeError("Repeated pagination cursor")
            seen.add(cursor)
            params = {**params, "cursor": cursor}

    def result(self, run: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while run["status"] in {"pending", "running"}:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Keep polling /v1/runs/{run['id']}; the run was not cancelled."
                )
            time.sleep(0.2)
            run = self.request("GET", f"/v1/runs/{run['id']}", replay_safe=True)
        if run["status"] != "succeeded":
            raise RuntimeError(
                f"Run {run['id']}: {run['status']} ({run['error_code']})"
            )
        return self.request("GET", f"/v1/results/{run['result_id']}", replay_safe=True)
