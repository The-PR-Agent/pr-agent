"""Every webhook server authenticates the request before parsing its body.

`github_app.get_body` already did this: read the raw bytes, verify the signature, and only
then parse. Four servers did the opposite, so an unauthenticated request got a free JSON
parse and, in three of them, a log line holding the whole body before the check ran.
"""

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTasks
from starlette.responses import Response
from starlette_context import context, request_cycle_context

from pr_agent.servers import bitbucket_server_webhook, gitea_app, gitlab_webhook
from pr_agent.servers.utils import payload_log_summary

WEBHOOK_SECRET = "test-webhook-secret"
SECRET_SENTINEL = "private-title-sentinel"


class _RecordingLogger:
    def __init__(self):
        self.calls = []

    def __getattr__(self, level):
        def record(*args, **kwargs):
            self.calls.append((level, args, kwargs))

        return record

    @property
    def logged(self):
        return repr(self.calls)


class _Request:
    """A request double that counts body reads and records whether the body was parsed."""

    def __init__(self, payload, headers=None, body=None):
        self.headers = headers or {}
        self._payload = payload
        self._body = json.dumps(payload).encode() if body is None else body
        self.json_calls = 0
        self.body_calls = 0

    async def json(self):
        self.json_calls += 1
        return self._payload

    async def body(self):
        self.body_calls += 1
        return self._body


def _endpoint(module, path="/webhook"):
    return next(route.endpoint for route in module.router.routes if route.path == path)


def _settings(values):
    return SimpleNamespace(get=lambda key, default=None: values.get(key, default))


def _bitbucket_server_settings(**overrides):
    values = {
        "BITBUCKET_SERVER.WEBHOOK_SECRET": WEBHOOK_SECRET,
        "BITBUCKET_SERVER.URL": "https://bb.example",
        "BITBUCKET_SERVER.HANDLE_PUSH_TRIGGER": False,
    }
    values.update(overrides)
    return _settings(values)


def _digest(body, secret=WEBHOOK_SECRET):
    """The raw hex digest; Gitea prefixes it itself, Bitbucket Server does not."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _sign(body, secret=WEBHOOK_SECRET):
    """The header value `verify_signature` expects."""
    return f"sha256={_digest(body, secret)}"


# payload_log_summary: the log shape shared by every server after #3818.


def test_summary_keeps_keys_and_identifying_fields_only():
    payload = {
        "eventKey": "pr:opened",
        "comment": {"text": SECRET_SENTINEL},
        "pullRequest": {"title": SECRET_SENTINEL},
    }

    summary = payload_log_summary(payload, ("eventKey",))

    assert summary == {"payload_keys": ["comment", "eventKey", "pullRequest"], "eventKey": "pr:opened"}
    assert SECRET_SENTINEL not in repr(summary)


@pytest.mark.parametrize(
    ("payload", "identifying", "expected"),
    [
        ({"eventKey": 42}, ("eventKey",), {"payload_keys": ["eventKey"]}),
        ({"eventKey": None}, ("eventKey",), {"payload_keys": ["eventKey"]}),
        ({"eventKey": ["pr:opened"]}, ("eventKey",), {"payload_keys": ["eventKey"]}),
        ({"eventKey": "pr:opened"}, ("absent",), {"payload_keys": ["eventKey"]}),
    ],
)
def test_summary_skips_absent_and_non_string_identifying_fields(payload, identifying, expected):
    assert payload_log_summary(payload, identifying) == expected


@pytest.mark.parametrize("payload", [["not", "an", "object"], "a string", 7, None])
def test_summary_reports_the_type_of_a_non_object(payload):
    assert payload_log_summary(payload) == {"payload_type": type(payload).__name__}


# Gitea: the signature is checked against the raw bytes, so parsing follows verification.


async def test_gitea_rejects_a_bad_signature_before_parsing(monkeypatch):
    logger = _RecordingLogger()
    request = _Request({"action": "opened"}, headers={"x-gitea-signature": "0" * 64})
    monkeypatch.setattr(gitea_app, "get_logger", lambda: logger)
    monkeypatch.setattr(gitea_app, "get_settings", lambda: _gitea_settings(WEBHOOK_SECRET))

    tasks = BackgroundTasks()
    with pytest.raises(Exception) as caught:
        await gitea_app.handle_gitea_webhooks(tasks, request, Response())

    assert caught.value.status_code == 401
    assert request.json_calls == 0
    assert tasks.tasks == []
    assert "opened" not in logger.logged


async def test_gitea_rejects_an_unconfigured_secret_before_reading_the_body(monkeypatch):
    logger = _RecordingLogger()
    request = _Request({"action": "opened"}, headers={"x-gitea-signature": "0" * 64})
    monkeypatch.setattr(gitea_app, "get_logger", lambda: logger)
    monkeypatch.setattr(gitea_app, "get_settings", lambda: _gitea_settings(""))

    tasks = BackgroundTasks()
    with pytest.raises(Exception) as caught:
        await gitea_app.handle_gitea_webhooks(tasks, request, Response())

    assert caught.value.status_code == 403
    # The bytes are read (verification needs them, as in github_app) but never parsed.
    assert request.json_calls == 0
    assert tasks.tasks == []


async def test_gitea_rejects_a_missing_signature_header_before_parsing(monkeypatch):
    logger = _RecordingLogger()
    request = _Request({"action": "opened"}, headers={})
    monkeypatch.setattr(gitea_app, "get_logger", lambda: logger)
    monkeypatch.setattr(gitea_app, "get_settings", lambda: _gitea_settings(WEBHOOK_SECRET))

    tasks = BackgroundTasks()
    with pytest.raises(Exception) as caught:
        await gitea_app.handle_gitea_webhooks(tasks, request, Response())

    assert caught.value.status_code == 400
    assert request.json_calls == 0
    assert tasks.tasks == []


async def test_gitea_parses_the_bytes_that_were_signed(monkeypatch):
    payload = {"action": "opened"}
    body = json.dumps(payload).encode()
    request = _Request(
        payload,
        headers={"x-gitea-signature": _digest(body), "X-Gitea-Event": "pull_request"},
        body=body,
    )
    delivered = []

    monkeypatch.setattr(gitea_app, "get_settings", lambda: _gitea_settings(WEBHOOK_SECRET))
    monkeypatch.setattr(gitea_app, "global_settings", _gitea_settings(WEBHOOK_SECRET))
    monkeypatch.setattr(gitea_app, "context", {})
    monkeypatch.setattr(
        gitea_app,
        "handle_request",
        lambda body, event: delivered.append((body, event)),
    )

    tasks = BackgroundTasks()
    assert await gitea_app.handle_gitea_webhooks(tasks, request, Response()) == {}
    await tasks()

    assert delivered == [(payload, "pull_request")]
    # Parsed from the verified bytes rather than read from the request a second time.
    assert request.json_calls == 0


def _gitea_settings(secret):
    return SimpleNamespace(gitea=SimpleNamespace(webhook_secret=secret))


# GitLab: authenticate first, and never log the body.


async def test_gitlab_rejects_an_unauthorized_request_without_reading_the_body(monkeypatch):
    logger = _RecordingLogger()
    request = _Request(
        {"object_kind": "merge_request", "title": SECRET_SENTINEL},
        headers={"X-Gitlab-Token": "wrong-token"},
    )
    monkeypatch.setattr(gitlab_webhook, "get_logger", lambda: logger)

    tasks = BackgroundTasks()
    with request_cycle_context({}):
        response = await _endpoint(gitlab_webhook)(tasks, request)

    assert response.status_code == 401
    assert request.json_calls == 0
    assert request.body_calls == 0
    assert tasks.tasks == []
    assert SECRET_SENTINEL not in logger.logged


async def test_gitlab_never_logs_the_payload_body(monkeypatch):
    logger = _RecordingLogger()
    payload = {"object_kind": "merge_request", "object_attributes": {"title": SECRET_SENTINEL}}
    request = _Request(payload, headers={})
    monkeypatch.setattr(gitlab_webhook, "get_logger", lambda: logger)
    monkeypatch.setattr(gitlab_webhook, "authenticate_gitlab_webhook", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(gitlab_webhook, "is_bot_user", lambda data: True)

    tasks = BackgroundTasks()
    with request_cycle_context({}):
        await _endpoint(gitlab_webhook)(tasks, request)
        await tasks()

    assert SECRET_SENTINEL not in logger.logged
    assert "object_kind" in logger.logged


async def test_gitlab_reports_a_malformed_body_after_authentication(monkeypatch):
    logger = _RecordingLogger()
    request = _Request(None, headers={}, body=b"not json")
    monkeypatch.setattr(gitlab_webhook, "get_logger", lambda: logger)
    monkeypatch.setattr(gitlab_webhook, "authenticate_gitlab_webhook", lambda *args, **kwargs: (None, None))

    tasks = BackgroundTasks()
    with request_cycle_context({}):
        response = await _endpoint(gitlab_webhook)(tasks, request)

    assert response.status_code == 400
    assert tasks.tasks == []


async def test_gitlab_installs_the_settings_copy_only_once_authenticated(monkeypatch):
    """The copy is installed after authentication, and carries the token the secret resolved."""
    payload = {"object_kind": "merge_request"}
    request = _Request(payload, headers={})
    monkeypatch.setattr(gitlab_webhook, "get_logger", _RecordingLogger)
    monkeypatch.setattr(
        gitlab_webhook, "authenticate_gitlab_webhook", lambda *args, **kwargs: (None, "secret-provider-token")
    )
    monkeypatch.setattr(gitlab_webhook, "is_bot_user", lambda data: True)
    monkeypatch.setattr(gitlab_webhook, "global_settings", _gitlab_host_settings())

    tasks = BackgroundTasks()
    with request_cycle_context({}):
        await _endpoint(gitlab_webhook)(tasks, request)
        installed = context["settings"]

    assert installed.gitlab.personal_access_token == "secret-provider-token"


async def test_gitlab_leaves_no_settings_copy_behind_a_rejected_request(monkeypatch):
    request = _Request({"object_kind": "merge_request"}, headers={})
    monkeypatch.setattr(gitlab_webhook, "get_logger", _RecordingLogger)
    monkeypatch.setattr(
        gitlab_webhook,
        "authenticate_gitlab_webhook",
        lambda *args, **kwargs: (
            JSONResponse(status_code=401, content={"message": "unauthorized"}),
            None,
        ),
    )
    monkeypatch.setattr(gitlab_webhook, "global_settings", _gitlab_host_settings())

    tasks = BackgroundTasks()
    with request_cycle_context({}):
        response = await _endpoint(gitlab_webhook)(tasks, request)
        assert "settings" not in context

    assert response.status_code == 401
    assert tasks.tasks == []


def _gitlab_host_settings():
    return SimpleNamespace(gitlab=SimpleNamespace(personal_access_token="host-token"))


# Bitbucket Server: the whole payload used to be logged at INFO before verification.


async def test_bitbucket_server_rejects_a_bad_signature_before_parsing(monkeypatch):
    logger = _RecordingLogger()
    payload = {"eventKey": "pr:opened", "pullRequest": {"title": SECRET_SENTINEL}}
    request = _Request(
        payload,
        headers={"x-hub-signature": "0" * 64},
        body=json.dumps(payload).encode(),
    )
    monkeypatch.setattr(bitbucket_server_webhook, "get_logger", lambda: logger)
    monkeypatch.setattr(
        bitbucket_server_webhook,
        "get_settings",
        _bitbucket_server_settings,
    )

    tasks = BackgroundTasks()
    with request_cycle_context({}), pytest.raises(HTTPException) as caught:
        await _endpoint(bitbucket_server_webhook)(tasks, request)

    assert caught.value.status_code == 403
    assert request.json_calls == 0
    assert tasks.tasks == []
    assert SECRET_SENTINEL not in logger.logged


async def test_bitbucket_server_rejects_every_webhook_without_a_configured_secret(monkeypatch):
    """An unconfigured secret used to accept anything, so the signature check never ran."""
    logger = _RecordingLogger()
    payload = {"eventKey": "pr:opened", "pullRequest": {"title": SECRET_SENTINEL}}
    request = _Request(payload, headers={"x-hub-signature": _sign(json.dumps(payload).encode())})
    monkeypatch.setattr(bitbucket_server_webhook, "get_logger", lambda: logger)
    monkeypatch.setattr(
        bitbucket_server_webhook,
        "get_settings",
        lambda: _bitbucket_server_settings(**{"BITBUCKET_SERVER.WEBHOOK_SECRET": ""}),
    )

    tasks = BackgroundTasks()
    with request_cycle_context({}):
        response = await _endpoint(bitbucket_server_webhook)(tasks, request)

    assert response.status_code == 403
    assert request.json_calls == 0
    assert tasks.tasks == []
    assert SECRET_SENTINEL not in logger.logged


async def test_bitbucket_server_logs_only_the_event_key_of_a_verified_payload(monkeypatch):
    logger = _RecordingLogger()
    payload = {
        "eventKey": "pr:comment:added",
        "comment": {"text": SECRET_SENTINEL},
        "pullRequest": {
            "id": 1,
            "toRef": {"repository": {"slug": "repo", "project": {"key": "project"}}},
        },
    }
    body = json.dumps(payload).encode()
    request = _Request(payload, headers={"x-hub-signature": _sign(body)}, body=body)
    monkeypatch.setattr(bitbucket_server_webhook, "get_logger", lambda: logger)
    monkeypatch.setattr(
        bitbucket_server_webhook,
        "get_settings",
        _bitbucket_server_settings,
    )
    monkeypatch.setattr(
        bitbucket_server_webhook,
        "apply_repo_settings",
        lambda url: None,
    )
    monkeypatch.setattr(
        bitbucket_server_webhook,
        "_run_commands_sequentially",
        lambda *args, **kwargs: None,
    )

    tasks = BackgroundTasks()
    with request_cycle_context({}):
        response = await _endpoint(bitbucket_server_webhook)(tasks, request)

    assert response.status_code == 200
    assert SECRET_SENTINEL not in logger.logged
    assert "pr:comment:added" in logger.logged


async def test_bitbucket_server_keeps_the_connection_test_pass(monkeypatch):
    """The '{\"test\": true}' probe is answered on the raw bytes, before any verification."""
    request = _Request(None, headers={}, body=b'{"test": true}')
    monkeypatch.setattr(
        bitbucket_server_webhook,
        "get_settings",
        _bitbucket_server_settings,
    )

    tasks = BackgroundTasks()
    with request_cycle_context({}):
        response = await _endpoint(bitbucket_server_webhook)(tasks, request)

    assert response.status_code == 200
    assert json.loads(response.body) == {"message": "connection test successful"}
    assert tasks.tasks == []
