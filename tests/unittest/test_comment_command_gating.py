import json
from unittest import mock

import pytest
from starlette.background import BackgroundTasks
from starlette_context import request_cycle_context

import pr_agent.servers.gitlab_webhook as gitlab_webhook
from pr_agent.config_loader import global_settings
from pr_agent.identity_providers.identity_provider import Eligibility
from pr_agent.servers import bitbucket_app, bitbucket_server_webhook
from pr_agent.servers.utils import is_command_comment


class _Request:
    def __init__(self, payload, headers=None):
        self.headers = headers if headers is not None else {
            "authorization": "JWT e30.eyJpc3MiOiJjbGllbnQifQ.signature"
        }
        self._payload = payload

    async def json(self):
        return self._payload

    async def body(self):
        return json.dumps(self._payload).encode()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("/review", True),
        ("/review --pr_reviewer.extra_instructions='focus on tests'", True),
        ("  /improve  ", True),
        ("/answer because the cache stores ids", True),
        ("review looks good to me", False),
        ("Ask Bob about this PR", False),
        ("nice catch, /ask about this later", False),
        ("", False),
        ("   ", False),
        (None, False),
        (12345, False),
    ],
)
def test_is_command_comment(body, expected):
    assert is_command_comment(body) is expected


async def _run_gitlab_note_webhook(monkeypatch, note_body, note_type=None):
    dispatched = []

    async def record_request(api_url, body, log_context, sender_id, notify=None):
        dispatched.append((api_url, body))

    object_attributes = {
        "id": 7,
        "note": note_body,
    }
    if note_type is not None:
        object_attributes["discussion_id"] = "discussion-1"
        object_attributes["type"] = note_type
        object_attributes["position"] = {
            "new_path": "src/app.py",
            "line_range": {
                "start": {"type": "new", "new_line": 10, "old_line": None},
                "end": {"type": "new", "new_line": 12, "old_line": None},
            },
        }
    payload = {
        "object_kind": "note",
        "event_type": "note",
        "user": {"username": "human-user", "id": 42, "name": "Human User"},
        "merge_request": {"url": "https://gitlab.example.com/group/repo/-/merge_requests/1"},
        "object_attributes": object_attributes,
    }

    monkeypatch.setattr(gitlab_webhook, "authenticate_gitlab_webhook", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gitlab_webhook, "get_git_provider_with_context", lambda **_kwargs: mock.MagicMock())
    monkeypatch.setattr(gitlab_webhook, "handle_request", record_request)

    endpoint = next(route.endpoint for route in gitlab_webhook.router.routes if route.path == "/webhook")
    background_tasks = BackgroundTasks()
    with request_cycle_context({}):
        await endpoint(background_tasks, _Request(payload, headers={}))
        await background_tasks()
    return dispatched


async def test_gitlab_plain_comment_does_not_dispatch(monkeypatch):
    dispatched = await _run_gitlab_note_webhook(monkeypatch, "review looks good to me")

    assert dispatched == []


async def test_gitlab_slash_command_dispatches(monkeypatch):
    dispatched = await _run_gitlab_note_webhook(monkeypatch, "/review focus on tests")

    assert dispatched == [("https://gitlab.example.com/group/repo/-/merge_requests/1", "/review focus on tests")]


async def test_gitlab_diffnote_with_embedded_ask_is_ignored(monkeypatch):
    dispatched = await _run_gitlab_note_webhook(
        monkeypatch, "can you /ask about this line?", note_type="DiffNote")

    assert dispatched == []


async def test_gitlab_diffnote_starting_with_ask_still_routes_to_ask_line(monkeypatch):
    dispatched = await _run_gitlab_note_webhook(monkeypatch, "/ask why is this null?", note_type="DiffNote")

    assert len(dispatched) == 1
    _url, body = dispatched[0]
    assert body[0] == "/ask_line"
    assert "--file_name=src/app.py" in body
    assert "why is this null?" in body


async def _run_bitbucket_comment_webhook(monkeypatch, comment_body):
    dispatched = []
    payload = {
        "event": "pullrequest:comment_created",
        "data": {
            "actor": {"type": "user", "account_id": "account"},
            "pullrequest": {"links": {"html": {"href": "https://example.test/pr/1"}}},
            "comment": {"content": {"raw": comment_body}},
        },
    }
    secret_provider = type(
        "SecretProvider",
        (),
        {"get_secret": lambda self, _key: json.dumps({"shared_secret": "secret"})},
    )()

    async def get_bearer_token(_shared_secret, _client_key):
        return "bearer"

    class FakeAgent:
        async def handle_request(self, pr_url, command, notify=None):
            dispatched.append((pr_url, command))

    class EligibleIdentityProvider:
        def verify_eligibility(self, *_args):
            return Eligibility.ELIGIBLE

    monkeypatch.setattr(bitbucket_app, "is_bot_user", lambda _data: False)
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: secret_provider)
    monkeypatch.setattr(bitbucket_app, "get_bearer_token", get_bearer_token)
    monkeypatch.setattr(bitbucket_app.jwt, "decode", lambda *args, **kwargs: {})
    monkeypatch.setattr(bitbucket_app, "get_identity_provider", EligibleIdentityProvider)
    monkeypatch.setattr(bitbucket_app, "PRAgent", FakeAgent)

    endpoint = next(route.endpoint for route in bitbucket_app.router.routes if route.path == "/webhook")
    background_tasks = BackgroundTasks()
    original_bitbucket = global_settings.get("BITBUCKET", {})
    global_settings.set("BITBUCKET.BASE_URL", "https://example.test/app")
    try:
        with request_cycle_context({}):
            result = await endpoint(background_tasks, _Request(payload))
            await background_tasks()
    finally:
        global_settings.set("BITBUCKET", original_bitbucket)
    return result, dispatched


async def test_bitbucket_app_plain_comment_does_not_dispatch(monkeypatch):
    result, dispatched = await _run_bitbucket_comment_webhook(monkeypatch, "review looks good to me")

    assert result == "OK"
    assert dispatched == []


async def test_bitbucket_app_slash_command_dispatches(monkeypatch):
    result, dispatched = await _run_bitbucket_comment_webhook(monkeypatch, "/review focus on tests")

    assert result == "OK"
    assert dispatched == [("https://example.test/pr/1", "/review focus on tests")]


async def _run_bitbucket_server_comment_webhook(monkeypatch, comment_text):
    recorded = []

    async def record_commands(commands, _url, _log_context):
        recorded.extend(commands)

    payload = {
        "eventKey": "pr:comment:added",
        "comment": {"text": comment_text},
        "pullRequest": {
            "id": 1,
            "toRef": {"repository": {"slug": "repo", "project": {"key": "project"}}},
        },
    }

    monkeypatch.setattr(bitbucket_server_webhook, "_run_commands_sequentially", record_commands)

    endpoint = next(
        route.endpoint for route in bitbucket_server_webhook.router.routes if route.path == "/webhook"
    )
    background_tasks = BackgroundTasks()
    with request_cycle_context({}):
        response = await endpoint(background_tasks, _Request(payload, headers={}))
        await background_tasks()
    return response, recorded


async def test_bitbucket_server_plain_comment_does_not_dispatch(monkeypatch):
    response, recorded = await _run_bitbucket_server_comment_webhook(monkeypatch, "review looks good to me")

    assert response.status_code == 200
    assert recorded == []


async def test_bitbucket_server_slash_command_dispatches(monkeypatch):
    response, recorded = await _run_bitbucket_server_comment_webhook(monkeypatch, "/review focus on tests")

    assert response.status_code == 200
    assert recorded == ["/review focus on tests"]
