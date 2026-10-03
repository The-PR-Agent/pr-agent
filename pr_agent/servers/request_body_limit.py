from typing import Iterable, Optional

from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.responses import JSONResponse

from pr_agent.config_loader import get_settings

DEFAULT_MAX_WEBHOOK_REQUEST_BODY_BYTES = 5 * 1024 * 1024


class RequestBodyLimitMiddleware:
    """Reject oversized HTTP bodies before application code parses them."""

    def __init__(self, app, max_body_size: int):
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = dict(scope.get("headers", [])).get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_body_size:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                pass

        body_chunks = []
        body_size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue

            chunk = message.get("body", b"")
            body_size += len(chunk)
            if body_size > self.max_body_size:
                await self._reject(scope, receive, send)
                return
            if chunk:
                body_chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body_index = 0
        empty_body_sent = False

        async def replay_body():
            nonlocal body_index, empty_body_sent
            if body_index < len(body_chunks):
                chunk = body_chunks[body_index]
                body_index += 1
                return {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": body_index < len(body_chunks),
                }
            if not body_chunks and not empty_body_sent:
                empty_body_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay_body, send)

    async def _reject(self, scope, receive, send):
        response = JSONResponse(status_code=413, content={"detail": "Request body too large"})
        await response(scope, receive, send)


def create_server_app(
    middleware: Optional[Iterable[Middleware]] = None,
    max_body_size: Optional[int] = None,
) -> FastAPI:
    """Build a FastAPI server with the shared request-body limit enabled."""
    if max_body_size is None:
        max_body_size = get_settings().get(
            "CONFIG.MAX_WEBHOOK_REQUEST_BODY_BYTES", DEFAULT_MAX_WEBHOOK_REQUEST_BODY_BYTES
        )
    try:
        max_body_size = int(max_body_size)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_webhook_request_body_bytes must be a positive integer") from exc
    if max_body_size <= 0:
        raise ValueError("max_webhook_request_body_bytes must be a positive integer")

    configured_middleware = [Middleware(RequestBodyLimitMiddleware, max_body_size=max_body_size)]
    configured_middleware.extend(middleware or [])
    return FastAPI(middleware=configured_middleware)
