import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.background import BackgroundTasks
from starlette.responses import Response
from starlette_context import request_cycle_context

from pr_agent.servers import gitea_app, gitlab_webhook

SENTINEL = "private-title-sentinel"


class _Logger:
    def __init__(self):
        self.calls = []

    def __getattr__(self, level):
        return lambda *args, **kwargs: self.calls.append((level, args, kwargs))


class _Request:
    def __init__(self, body: bytes, headers=None):
        self.headers = headers or {}
        self._body = body

    async def body(self):
        return self._body

    async def json(self):
        return json.loads(self._body)


async def test_gitea_rejects_bad_signature_before_parsing(monkeypatch):
    settings = SimpleNamespace(gitea=SimpleNamespace(webhook_secret="secret"))
    monkeypatch.setattr(gitea_app, "get_settings", lambda: settings)
    request = _Request(b"not json", {"x-gitea-signature": "0" * 64})

    with pytest.raises(HTTPException) as caught:
        await gitea_app.handle_gitea_webhooks(BackgroundTasks(), request, Response())

    assert caught.value.status_code == 401


async def test_gitlab_rejects_bad_token_before_parsing(monkeypatch):
    settings = SimpleNamespace(get=lambda key, default=None: {"GITLAB.SHARED_SECRET": "secret"}.get(key, default))
    monkeypatch.setattr(gitlab_webhook, "get_settings", lambda: settings)
    request = _Request(b"not json", {"X-Gitlab-Token": "wrong"})

    with request_cycle_context({}):
        response = await gitlab_webhook.gitlab_webhook(BackgroundTasks(), request)

    assert response.status_code == 401


async def test_gitlab_does_not_log_the_payload(monkeypatch):
    logger = _Logger()
    monkeypatch.setattr(gitlab_webhook, "get_logger", lambda: logger)
    monkeypatch.setattr(gitlab_webhook, "authenticate_gitlab_webhook", lambda *args: None)
    request = _Request(json.dumps({"object_kind": "push", "title": SENTINEL}).encode())
    tasks = BackgroundTasks()

    with request_cycle_context({}):
        await gitlab_webhook.gitlab_webhook(tasks, request)
        await tasks()

    assert "push" in repr(logger.calls)
    assert SENTINEL not in repr(logger.calls)

