from typing import Iterable, Optional

from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.responses import JSONResponse

from pr_agent.config_loader import get_settings


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
        try:
            declared_content_length = int(content_length) if content_length is not None else None
        except ValueError:
            # Fall back to streamed byte counting; never trust an invalid size.
            declared_content_length = None
        if declared_content_length is not None and declared_content_length > self.max_body_size:
            await self._reject(scope, receive, send)
            return

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

        body = b"".join(body_chunks)
        replayed = False

        async def replay_body():
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay_body, send)

    async def _reject(self, scope, receive, send):
        response = JSONResponse(status_code=413, content={"detail": "Request body too large"})
        await response(scope, receive, send)


def get_max_request_body_size(max_body_size: Optional[int] = None) -> int:
    """Resolve the shared positive body limit for FastAPI and Starlette servers."""
    if max_body_size is None:
        max_body_size = get_settings().config.max_webhook_request_body_bytes
    try:
        max_body_size = int(max_body_size)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_webhook_request_body_bytes must be a positive integer") from exc
    if max_body_size <= 0:
        raise ValueError("max_webhook_request_body_bytes must be a positive integer")

    return max_body_size


def create_server_app(
    middleware: Optional[Iterable[Middleware]] = None,
    max_body_size: Optional[int] = None,
) -> FastAPI:
    """Build a FastAPI server with the shared request-body limit enabled."""
    max_body_size = get_max_request_body_size(max_body_size)
    configured_middleware = [Middleware(RequestBodyLimitMiddleware, max_body_size=max_body_size)]
    configured_middleware.extend(middleware or [])
    return FastAPI(middleware=configured_middleware)
