import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from pr_agent.servers import github_app
from pr_agent.servers.request_body_limit import RequestBodyLimitMiddleware, create_server_app


async def _send_request(app, headers, body_chunks):
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(body_chunks) - 1,
        }
        for index, chunk in enumerate(body_chunks)
    ]
    receive_count = 0
    sent = []

    async def receive():
        nonlocal receive_count
        receive_count += 1
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/webhook",
        "raw_path": b"/webhook",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "state": {},
    }
    await app(scope, receive, send)
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    return status, receive_count


def test_rejects_content_length_over_limit_before_calling_handler():
    app = create_server_app(max_body_size=8)
    handler_called = False

    @app.post("/webhook")
    async def webhook(request: Request):
        nonlocal handler_called
        handler_called = True
        return await request.json()

    status, receive_count = asyncio.run(
        _send_request(
            app,
            [(b"host", b"testserver"), (b"content-length", b"9")],
            [b"not json!"],
        )
    )

    assert status == 413
    assert receive_count == 0
    assert not handler_called


def test_malformed_content_length_still_enforces_streamed_body_limit():
    app = create_server_app(max_body_size=8)
    handler_called = False

    @app.post("/webhook")
    async def webhook(request: Request):
        nonlocal handler_called
        handler_called = True
        return await request.body()

    status, _ = asyncio.run(
        _send_request(
            app,
            [(b"host", b"testserver"), (b"content-length", b"invalid")],
            [b"12345678", b"9"],
        )
    )

    assert status == 413
    assert not handler_called


def test_rejects_chunked_body_over_limit_before_calling_handler():
    app = create_server_app(max_body_size=8)
    handler_called = False

    @app.post("/webhook")
    async def webhook(request: Request):
        nonlocal handler_called
        handler_called = True
        return await request.json()

    status, receive_count = asyncio.run(
        _send_request(
            app,
            [(b"host", b"testserver")],
            [b'{"ok":', b"123}"],
        )
    )

    assert status == 413
    assert receive_count == 2
    assert not handler_called


def test_allows_body_at_limit_and_replays_it_to_handler():
    app = create_server_app(max_body_size=8)
    handler_called = False

    @app.post("/webhook")
    async def webhook(request: Request):
        nonlocal handler_called
        handler_called = True
        payload = await request.json()
        return {"received": payload["ok"]}

    status, _ = asyncio.run(
        _send_request(
            app,
            [(b"host", b"testserver"), (b"content-length", b"8")],
            [b'{"ok"', b":1}"],
        )
    )

    assert status == 200
    assert handler_called


def test_replays_buffered_body_as_one_message_and_continues_receiving():
    messages = [
        {"type": "http.request", "body": b"first ", "more_body": True},
        {"type": "http.request", "body": b"chunk", "more_body": False},
        {"type": "http.disconnect"},
    ]
    receive_count = 0
    forwarded = []

    async def receive():
        nonlocal receive_count
        receive_count += 1
        return messages.pop(0)

    async def app(scope, app_receive, send):
        forwarded.append(await app_receive())
        forwarded.append(await app_receive())

    async def send(_message):
        pass

    middleware = RequestBodyLimitMiddleware(app, max_body_size=11)
    scope = {"type": "http", "headers": []}

    asyncio.run(middleware(scope, receive, send))

    assert forwarded == [
        {"type": "http.request", "body": b"first chunk", "more_body": False},
        {"type": "http.disconnect"},
    ]
    assert receive_count == 3
    assert not messages


def _request(body):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/webhook",
            "raw_path": b"/webhook",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"x-hub-signature-256", b"sha256=invalid")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        },
        receive,
    )


def test_github_signature_is_verified_before_json_parsing(monkeypatch):
    monkeypatch.setattr(
        github_app,
        "get_settings",
        lambda: SimpleNamespace(github=SimpleNamespace(webhook_secret="secret")),
    )
    verified = []

    def reject_signature(body, secret, signature):
        verified.append((body, secret, signature))
        raise HTTPException(status_code=401, detail="invalid signature")

    monkeypatch.setattr(github_app, "verify_signature", reject_signature)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(github_app.get_body(_request(b"not json")))

    assert exc_info.value.status_code == 401
    assert verified == [(b"not json", "secret", "sha256=invalid")]


def test_github_still_rejects_malformed_json_after_valid_signature(monkeypatch):
    monkeypatch.setattr(
        github_app,
        "get_settings",
        lambda: SimpleNamespace(github=SimpleNamespace(webhook_secret="secret")),
    )
    verified = []
    monkeypatch.setattr(
        github_app,
        "verify_signature",
        lambda body, secret, signature: verified.append((body, secret, signature)),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(github_app.get_body(_request(b"not json")))

    assert exc_info.value.status_code == 400
    assert verified == [(b"not json", "secret", "sha256=invalid")]


@pytest.mark.parametrize(
    "module_name",
    [
        "azuredevops_server_webhook.py",
        "bitbucket_app.py",
        "bitbucket_server_webhook.py",
        "gerrit_server.py",
        "gitea_app.py",
        "github_app.py",
        "github_lambda_webhook.py",
        "gitlab_lambda_webhook.py",
        "gitlab_webhook.py",
    ],
)
def test_every_webhook_server_uses_the_shared_body_limit_factory(module_name):
    server_dir = Path(__file__).resolve().parents[2] / "pr_agent" / "servers"
    module = ast.parse((server_dir / module_name).read_text(encoding="utf-8"))
    calls = [node.func for node in ast.walk(module) if isinstance(node, ast.Call)]
    called_names = {
        function.id if isinstance(function, ast.Name) else function.attr
        for function in calls
        if isinstance(function, (ast.Name, ast.Attribute))
    }

    assert "create_server_app" in called_names
    assert "FastAPI" not in called_names
