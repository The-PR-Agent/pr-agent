import copy
import hashlib
import hmac
import json
from unittest import mock

import pytest
from starlette.background import BackgroundTasks
from starlette_context import context, request_cycle_context

import pr_agent.servers.gitea_app as gitea_app
import pr_agent.servers.gitlab_webhook as gitlab_webhook
from pr_agent.algo.utils import decode_user_text_args
from pr_agent.config_loader import global_settings
from pr_agent.identity_providers.identity_provider import Eligibility
from pr_agent.servers import bitbucket_app, bitbucket_server_webhook, github_app
from pr_agent.servers.github_common import handle_file_question_comment
from pr_agent.servers.utils import is_ask_command_comment, is_command_comment


class _Request:
    def __init__(self, payload, headers=None, method="POST", path="/webhook", query=""):
        self.headers = headers if headers is not None else {
            "authorization": "JWT e30.eyJpc3MiOiJjbGllbnQifQ.signature"
        }
        self._payload = payload
        self.method = method
        self.url = type("URL", (), {"path": path, "query": query})()

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


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("/ask why this line changed?", True),
        ("  /ask why this line changed?", True),
        ("/ask", True),
        ("/ask_line --line_start=1", True),
        ("/asking about retries", False),
        ("/askfoo", False),
        ("/ASK why?", True),
        ("/ask_question why?", True),
        ("/ASK_LINE why?", True),
        ("/review please, I will /ask later", False),
        ("review please, I will /ask later", False),
        ("can you /ask about this line?", False),
        ("", False),
        ("   ", False),
        (None, False),
        (12345, False),
    ],
)
def test_is_ask_command_comment(body, expected):
    assert is_ask_command_comment(body) is expected


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

    monkeypatch.setattr(gitlab_webhook, "authenticate_gitlab_webhook", mock.AsyncMock(return_value=None))
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


async def test_gitlab_diffnote_review_mentioning_ask_stays_review(monkeypatch):
    dispatched = await _run_gitlab_note_webhook(
        monkeypatch, "/review please, I will /ask later", note_type="DiffNote")

    assert dispatched == [(
        "https://gitlab.example.com/group/repo/-/merge_requests/1",
        "/review please, I will /ask later",
    )]


@pytest.mark.parametrize("command", ["/ask", "/ask_question", "/ASK", "/ask_line", "/ASK_LINE"])
async def test_gitlab_diffnote_starting_with_ask_still_routes_to_ask_line(monkeypatch, command):
    dispatched = await _run_gitlab_note_webhook(monkeypatch, f"{command} why is this null?", note_type="DiffNote")

    assert len(dispatched) == 1
    _url, body = dispatched[0]
    assert body[0] == "/ask_line"
    assert "--file_name=src/app.py" in body
    assert "why is this null?" in body


async def test_github_line_review_mentioning_ask_stays_review(monkeypatch):
    handled = []

    class FakeAgent:
        async def handle_request(self, api_url, body, notify=None, propagate_command_errors=False):
            handled.append((api_url, body))
            return True

    class FakeProvider:
        def add_eyes_reaction(self, comment_id, disable_eyes=False):
            return None

        def react_to_outcome(self, comment_id, succeeded):
            return None

    class EligibleIdentityProvider:
        def verify_eligibility(self, *_args):
            return Eligibility.ELIGIBLE

    body = {
        "action": "created",
        "comment": {
            "body": "/review please, I will /ask later",
            "id": 123,
            "pull_request_url": "https://api.github.com/repos/org/repo/pulls/1",
            "subject_type": "line",
            "start_line": 10,
            "line": 12,
            "diff_hunk": "@@ -1,3 +1,4 @@\n+new line",
            "path": "src/app.py",
            "side": "RIGHT",
        },
    }

    monkeypatch.setattr(github_app, "get_git_provider_with_context", lambda **_kwargs: FakeProvider())
    monkeypatch.setattr(github_app, "get_identity_provider", EligibleIdentityProvider)

    await github_app.handle_comments_on_pr(
        body, "pull_request_review_comment", "human-user", "42", "created", {}, FakeAgent())

    assert handled == [(
        "https://api.github.com/repos/org/repo/pulls/1",
        "/review please, I will /ask later",
    )]


async def test_github_line_comment_prefixing_ask_is_not_rewritten_to_ask_line(monkeypatch):
    handled = []

    class FakeAgent:
        async def handle_request(self, api_url, body, notify=None, propagate_command_errors=False):
            handled.append((api_url, body))
            return True

    class FakeProvider:
        def add_eyes_reaction(self, comment_id, disable_eyes=False):
            return None

        def react_to_outcome(self, comment_id, succeeded):
            return None

    class EligibleIdentityProvider:
        def verify_eligibility(self, *_args):
            return Eligibility.ELIGIBLE

    body = {
        "action": "created",
        "comment": {
            "body": "/asking about retries",
            "id": 123,
            "pull_request_url": "https://api.github.com/repos/org/repo/pulls/1",
            "subject_type": "line",
            "start_line": 10,
            "line": 12,
            "diff_hunk": "@@ -1,3 +1,4 @@\n+new line",
            "path": "src/app.py",
            "side": "RIGHT",
        },
    }

    monkeypatch.setattr(github_app, "get_git_provider_with_context", lambda **_kwargs: FakeProvider())
    monkeypatch.setattr(github_app, "get_identity_provider", EligibleIdentityProvider)

    await github_app.handle_comments_on_pr(
        body, "pull_request_review_comment", "human-user", "42", "created", {}, FakeAgent())

    # Not converted to an /ask_line argv list with the corrupted question "ing about retries".
    assert handled == [(
        "https://api.github.com/repos/org/repo/pulls/1",
        "/asking about retries",
    )]


FILE_QUESTION_CONTEXT_KEYS = (
    "file_question_path", "file_question_comment_id", "file_question_origin_comment_id", "file_question_comment_url",
)


@pytest.mark.parametrize(("subject_type", "command", "in_reply_to_id"), [
    ("file", "/ask", None), ("file", "/ask_question", None), ("file", "/ASK", 456), ("file", "/ask_line", None),
    ("line", "/ask", None), ("line", "/ask_line", None),
])
async def test_github_review_comment_question_routes_as_in_the_action(
    monkeypatch, subject_type, command, in_reply_to_id
):
    question = "Explain --config.publish_output=false in this code"
    handled = []
    reactions = []

    class FakeAgent:
        async def handle_request(self, api_url, body, notify=None, propagate_command_errors=False):
            notify()
            handled.append((body, {key: context.get(key) for key in FILE_QUESTION_CONTEXT_KEYS}))
            return True

    class FakeProvider:
        def add_eyes_reaction(self, comment_id, disable_eyes=False):
            reactions.append((comment_id, disable_eyes))

        def react_to_outcome(self, comment_id, succeeded):
            return None

    class EligibleIdentityProvider:
        def verify_eligibility(self, *_args):
            return Eligibility.ELIGIBLE

    comment = {
        "body": f"{command} {question}",
        "id": 123,
        "in_reply_to_id": in_reply_to_id,
        "html_url": "https://github.com/org/repo/pull/1#discussion_r123",
        "pull_request_url": "https://api.github.com/repos/org/repo/pulls/1",
        "subject_type": subject_type,
        "start_line": 10,
        "line": 12,
        "diff_hunk": "@@ -1,3 +1,4 @@\n+new line",
        "path": "src/app.py",
        "side": "RIGHT",
    }
    if subject_type == "file":
        # GitHub sends a comment on a whole file with a placeholder line 1 and an empty diff hunk.
        comment.update(start_line=None, line=1, original_line=1, diff_hunk="")

    monkeypatch.setattr(github_app, "get_git_provider_with_context", lambda **_kwargs: FakeProvider())
    monkeypatch.setattr(github_app, "get_identity_provider", EligibleIdentityProvider)

    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        await github_app.handle_comments_on_pr(
            {"action": "created", "comment": comment}, "pull_request_review_comment", "human-user", "42",
            "created", {}, FakeAgent())

    [(request, file_question)] = handled
    if subject_type == "file" and command.lower() != "/ask_line":
        # A question on a whole file is asked about that file and answered in its thread, as in the Action.
        assert request[0] == "/ask"
        assert decode_user_text_args(request[1:]) == question
        assert file_question == {
            "file_question_path": "src/app.py",
            "file_question_comment_id": in_reply_to_id or 123,
            "file_question_origin_comment_id": 123,
            "file_question_comment_url": "https://github.com/org/repo/pull/1#discussion_r123",
        }
    else:
        start, end = (1, 1) if subject_type == "file" else (10, 12)
        assert request == [
            "/ask_line", f"--line_start={start}", f"--line_end={end}", "--side=RIGHT",
            "--file_name=src/app.py", "--comment_id=123", question,
        ]
        assert file_question == dict.fromkeys(FILE_QUESTION_CONTEXT_KEYS)
    # No eyes reaction: the provider adds it through the issue comment endpoint, which has no review comments.
    assert reactions == [(123, True)]


@pytest.mark.parametrize(("overrides", "missing_field"), [
    ({"path": None}, None),
    ({"path": ""}, None),
    ({"path": "   "}, None),
    ({"path": 123}, None),
    ({}, "path"),
])
async def test_github_file_question_rejects_invalid_path(monkeypatch, overrides, missing_field):
    comment = {
        "body": "/ask Why this file?",
        "subject_type": "file",
        "path": "src/app.py",
        "id": 123,
        "in_reply_to_id": 456,
        "html_url": "https://github.com/org/repo/pull/1#discussion_r123",
        "pull_request_url": "https://api.github.com/repos/org/repo/pulls/1",
    }
    comment.update(overrides)
    if missing_field:
        comment.pop(missing_field)

    def unexpected_provider(**_kwargs):
        pytest.fail("Invalid file questions must not reach the agent")

    monkeypatch.setattr(github_app, "get_git_provider_with_context", unexpected_provider)

    with request_cycle_context({}):
        # The Action calls this same helper and stops processing when it raises.
        with pytest.raises(ValueError, match="Invalid file-level review comment metadata"):
            handle_file_question_comment({"comment": comment}, comment["body"])
        assert all(context.get(key) is None for key in FILE_QUESTION_CONTEXT_KEYS)

        # The App must not continue with an unscoped /ask after catching the error.
        await github_app.handle_comments_on_pr(
            {"action": "created", "comment": comment}, "pull_request_review_comment", "human-user", "42",
            "created", {}, mock.AsyncMock(),
        )
        assert all(context.get(key) is None for key in FILE_QUESTION_CONTEXT_KEYS)


@pytest.mark.parametrize(("subject_type", "command"), [
    ("file", "/ask_line"),
    ("line", "/ask"),
    ("line", "/ask_line"),
])
async def test_github_review_comment_context_error_does_not_dispatch(monkeypatch, subject_type, command):
    comment = {
        "body": f"{command} Why this line?",
        "subject_type": subject_type,
        "id": 123,
        "start_line": None,
        "line": 1,
        "diff_hunk": "",
        "side": "RIGHT",
        "pull_request_url": "https://api.github.com/repos/org/repo/pulls/1",
        # Missing path causes handle_line_comments to fail.
    }

    def unexpected_provider(**_kwargs):
        pytest.fail("Malformed review comment must not be dispatched")

    monkeypatch.setattr(github_app, "get_git_provider_with_context", unexpected_provider)
    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        await github_app.handle_comments_on_pr(
            {"action": "created", "comment": comment}, "pull_request_review_comment", "human-user", "42",
            "created", {}, mock.AsyncMock(),
        )
        assert all(context.get(key) is None for key in FILE_QUESTION_CONTEXT_KEYS)


async def _run_bitbucket_comment_webhook(
    monkeypatch, comment_body, *, event="pullrequest:comment_created", bot=False, process_pr=True, token_error=None
):
    dispatched = []
    work = {"tokens": 0, "settings_clones": 0, "agents": 0}
    payload = {
        "event": event,
        "data": {
            "actor": {"type": "user", "account_id": "account"},
            "pullrequest": {"links": {"html": {"href": "https://example.test/pr/1"}}},
            "comment": {"content": {"raw": comment_body}},
        },
    }
    if event not in ("pullrequest:created", "pullrequest:updated", "pullrequest:comment_created"):
        payload["data"] = {"actor": payload["data"]["actor"]}
    secret_provider = type(
        "SecretProvider",
        (),
        {"get_secret": lambda self, _key: json.dumps({"shared_secret": "secret"})},
    )()

    async def get_bearer_token(shared_secret, client_key):
        assert (shared_secret, client_key) == ("secret", "client")
        work["tokens"] += 1
        if token_error:
            raise token_error
        return "bearer"

    def clone_settings(settings):
        work["settings_clones"] += 1
        return copy.deepcopy(settings)

    class FakeAgent:
        def __init__(self):
            work["agents"] += 1

        async def handle_request(self, pr_url, command, notify=None):
            dispatched.append((pr_url, command))

    async def perform_commands(category, agent, pr_url, _log_context, _data):
        assert isinstance(agent, FakeAgent)
        dispatched.append((pr_url, category))

    class EligibleIdentityProvider:
        def verify_eligibility(self, *_args):
            return Eligibility.ELIGIBLE

    monkeypatch.setattr(bitbucket_app, "is_bot_user", lambda _data: bot)
    monkeypatch.setattr(bitbucket_app, "should_process_pr_logic", lambda _data: process_pr)
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: secret_provider)
    monkeypatch.setattr(bitbucket_app, "get_bearer_token", get_bearer_token)
    monkeypatch.setattr(bitbucket_app, "copy", mock.Mock(deepcopy=clone_settings))
    monkeypatch.setattr(bitbucket_app, "_perform_commands_bitbucket", perform_commands)
    monkeypatch.setattr(
        bitbucket_app.jwt,
        "decode",
        lambda *args, **kwargs: {"qsh": bitbucket_app._compute_qsh("POST", "/webhook")},
    )
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
    return result, dispatched, work


@pytest.mark.parametrize("comment_body", ["review looks good to me", "   "])
async def test_bitbucket_app_plain_comment_does_not_dispatch(monkeypatch, comment_body):
    result, dispatched, work = await _run_bitbucket_comment_webhook(monkeypatch, comment_body)

    assert result == "OK"
    assert dispatched == []
    assert work == {"tokens": 0, "settings_clones": 0, "agents": 0}


@pytest.mark.parametrize("command", ["/review focus on tests", "  /improve  ", "/unknown-command"])
async def test_bitbucket_app_slash_command_dispatches(monkeypatch, command):
    result, dispatched, work = await _run_bitbucket_comment_webhook(monkeypatch, command)

    assert result == "OK"
    assert dispatched == [("https://example.test/pr/1", command)]
    assert work == {"tokens": 1, "settings_clones": 1, "agents": 1}


async def test_bitbucket_app_unsupported_event_skips_oauth_and_setup(monkeypatch):
    result, dispatched, work = await _run_bitbucket_comment_webhook(monkeypatch, None, event="repo:push")

    assert result == "OK"
    assert dispatched == []
    assert work == {"tokens": 0, "settings_clones": 0, "agents": 0}


@pytest.mark.parametrize(("event", "category"), [
    ("pullrequest:created", "pr_commands"),
    ("pullrequest:updated", "push_commands"),
])
async def test_bitbucket_app_actionable_event_keeps_oauth_and_dispatch(monkeypatch, event, category):
    result, dispatched, work = await _run_bitbucket_comment_webhook(monkeypatch, None, event=event)

    assert result == "OK"
    assert dispatched == [("https://example.test/pr/1", category)]
    assert work == {"tokens": 1, "settings_clones": 1, "agents": 1}


@pytest.mark.parametrize("gate", ["bot", "created-policy"])
async def test_bitbucket_app_existing_gate_still_skips_oauth(monkeypatch, gate):
    result, dispatched, work = await _run_bitbucket_comment_webhook(
        monkeypatch, None, event="pullrequest:created", bot=gate == "bot", process_pr=gate != "created-policy"
    )

    assert result == "OK"
    assert dispatched == []
    assert work == {"tokens": 0, "settings_clones": 0, "agents": 0}


async def test_bitbucket_app_actionable_token_failure_does_not_dispatch(monkeypatch):
    result, dispatched, work = await _run_bitbucket_comment_webhook(
        monkeypatch, "/review", token_error=RuntimeError("token unavailable")
    )

    assert result == "OK"
    assert dispatched == []
    assert work == {"tokens": 1, "settings_clones": 0, "agents": 0}


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
    settings = copy.deepcopy(global_settings)
    settings.set("BITBUCKET_SERVER.WEBHOOK_SECRET", "test-webhook-secret")
    request = _Request(payload, headers={})
    request.headers["x-hub-signature"] = "sha256=" + hmac.new(
        b"test-webhook-secret", await request.body(), hashlib.sha256,
    ).hexdigest()
    with request_cycle_context({"settings": settings}):
        response = await endpoint(background_tasks, request)
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


async def _run_gitea_comment_event(comment_body):
    dispatched = []

    class FakeAgent:
        async def handle_request(self, pr_url, command, notify=None):
            dispatched.append((pr_url, command))

    body = {
        "comment": {"body": comment_body},
        "pull_request": {"url": "https://example.test/api/v1/repos/o/r/pulls/1"},
    }
    await gitea_app.handle_comment_event(body, "comment", "created", FakeAgent())
    return dispatched


async def test_gitea_slash_command_dispatches():
    assert await _run_gitea_comment_event("/review focus on tests") == [
        ("https://example.test/api/v1/repos/o/r/pulls/1", "/review focus on tests")
    ]


async def test_gitea_indented_slash_command_dispatches():
    """A command that is not flush left was dropped before, unlike every other provider."""
    assert await _run_gitea_comment_event("  /review  ") == [
        ("https://example.test/api/v1/repos/o/r/pulls/1", "  /review  ")
    ]


@pytest.mark.parametrize("comment_body", [
    "review looks good to me",
    "nice catch, /ask about this later",
    "",
    "   ",
    None,
])
async def test_gitea_plain_comment_does_not_dispatch(comment_body):
    assert await _run_gitea_comment_event(comment_body) == []
