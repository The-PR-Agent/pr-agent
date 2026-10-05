"""The GitLab webhook has to say how a comment command ended, in reactions on that comment.

The GitHub App handler already does this; the GitLab webhook only added the start reaction, so
`reaction_on_success` and `reaction_on_failure` did nothing there. The interesting part is not the
extra call but the verdict: `propagate_tool_errors` is off, so a tool that fails internally still
returns normally, and reacting as if it had succeeded would tick a comment whose command never
ran. `command_failed()` is the signal that does not change how the tool handles its own error.

Both outcome reactions default to empty, so this path is inert until an operator configures one;
`test_the_shipped_defaults_ask_for_no_outcome_reaction` below pins that.
"""

import asyncio
import json
import shlex
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.background import BackgroundTasks
from starlette_context import request_cycle_context

import pr_agent.agent.pr_agent as pr_agent_module
from pr_agent.algo.run_details import record_command_failure
from pr_agent.config_loader import get_settings
from tests.unittest._reaction_helpers import _RecordingProvider

COMMENT_ID = 4242


def _first_token(request):
    """The first token the way `PRAgent` reads one: shlex, with quotes handled.

    A comment the webhook rewrote (`/ask` on a diff line) arrives as argv rather than a string.
    """
    if isinstance(request, list):
        return request[0] if request else ""
    lexer = shlex.shlex(request, posix=True)
    lexer.whitespace_split = True
    lexer.quotes = '"'
    lexer.commenters = ""
    return next(iter(lexer), "")


def _acknowledges(request):
    """Whether `PRAgent` would call `notify` for this comment.

    It runs a command it knows and then acknowledges it, with one exception: `auto_review` is
    silent, because it runs on every merge request rather than on somebody asking for something.
    """
    action = _first_token(request).lstrip("/").lower()
    return action in pr_agent_module.command2class and action != "auto_review"


def _note_event(body="/review"):
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {"username": "reviewer", "id": 7},
        "object_attributes": {"id": COMMENT_ID, "note": body},
        "merge_request": {"url": "https://gitlab.example.com/group/repo/-/merge_requests/1"},
    }


def _diff_note_event(note):
    """A comment on a diff line, which is what makes the webhook rewrite `/ask` into `/ask_line`."""
    payload = _note_event(note)
    payload["object_attributes"].update({
        "type": "DiffNote",
        "discussion_id": "abc123",
        "position": {
            "new_path": "src/app.py",
            "line_range": {"start": {"type": "new", "new_line": 12},
                           "end": {"type": "new", "new_line": 14}},
        },
    })
    return payload


@pytest.fixture
def run_comment(monkeypatch):
    """Post a comment through the real webhook endpoint, faking only what talks to GitLab.

    ``success`` and ``failure`` are the operator's `reaction_on_success` / `reaction_on_failure`.
    ``outcome`` is what the agent reports, ``record_failure`` is a tool that failed internally and
    therefore returned normally.
    """
    import pr_agent.servers.gitlab_webhook as gitlab_webhook

    monkeypatch.setattr(gitlab_webhook, "is_bot_user", lambda data: False)
    monkeypatch.setitem(get_settings().gitlab, "personal_access_token", "token")
    monkeypatch.setitem(get_settings().gitlab, "shared_secret", "secret")
    client = TestClient(gitlab_webhook.app)

    def run(success=None, failure=None, outcome=True, record_failure=False, start=None,
            body="/review", payload=None):
        """Run one comment through the webhook.

        `success`, `failure` and `start` are left as the repository ships them unless a test asks
        for a value, so the test that pins the defaults reads the real configuration.
        """
        provider = _RecordingProvider()
        dispatched = []
        provider.dispatched = dispatched
        monkeypatch.setattr(gitlab_webhook, "get_git_provider_with_context", lambda pr_url: provider)
        for key, value in (("reaction_on_start", start),
                           ("reaction_on_success", success),
                           ("reaction_on_failure", failure)):
            if value is not None:
                monkeypatch.setattr(get_settings().config, key, value, raising=False)

        async def handle_request(api_url, request, log_context, sender_id, notify=None):
            dispatched.append(request)
            if notify and _acknowledges(request):
                notify()
            if record_failure:  # what a tool does just before swallowing its own error
                record_command_failure()
            return outcome

        monkeypatch.setattr(gitlab_webhook, "handle_request", handle_request)
        response = client.post("/webhook", json=payload if payload else _note_event(body),
                               headers={"X-Gitlab-Token": "secret"})
        assert response.status_code == 200, response.text
        return provider

    return run


def test_the_shipped_defaults_ask_for_no_outcome_reaction(run_comment):
    """Reads the shipped configuration, so a default that starts asking for a reaction fails here."""
    config = get_settings().config
    assert config.get("reaction_on_start", "") == "eyes"
    assert config.get("reaction_on_success", "") == "", "this test pins the no-op default"
    assert config.get("reaction_on_failure", "") == "", "this test pins the no-op default"

    provider = run_comment()

    assert provider.reactions == [(COMMENT_ID, "eyes")]
    assert provider.removed == [], "with no outcome reaction configured there is nothing to swap"


def test_a_successful_command_adds_the_success_reaction(run_comment):
    provider = run_comment(success="hooray")

    assert provider.reactions == [(COMMENT_ID, "eyes"), (COMMENT_ID, "hooray")]
    assert provider.removed == [(COMMENT_ID, 1)], "the start reaction is taken down first"


def test_a_command_that_reported_failure_adds_the_failure_reaction(run_comment):
    provider = run_comment(failure="confused", outcome=False)

    assert provider.reactions == [(COMMENT_ID, "eyes"), (COMMENT_ID, "confused")]
    assert provider.removed == [(COMMENT_ID, 1)]


def test_a_tool_that_failed_internally_is_not_reported_as_success(run_comment):
    """The trap this change exists to avoid: the tool swallowed its error and returned True."""
    provider = run_comment(success="hooray", failure="confused", record_failure=True)

    assert provider.reactions == [(COMMENT_ID, "eyes"), (COMMENT_ID, "confused")]


@pytest.mark.asyncio
async def test_outcome_reaction_preserves_settings_without_blocking_the_event_loop(monkeypatch):
    import pr_agent.servers.gitlab_webhook as gitlab_webhook

    provider = _RecordingProvider()
    started = threading.Event()
    released = threading.Event()
    original = provider.react_to_outcome

    def blocking_reaction(comment_id, succeeded):
        started.set()
        assert released.wait(5), "the event loop could not release the blocking reaction"
        return original(comment_id, succeeded)

    async def handle_request(api_url, body, log_context, sender_id, notify=None):
        notify()
        return True

    async def release_reaction():
        assert await asyncio.to_thread(started.wait, 5), "the outcome reaction never started"
        released.set()

    monkeypatch.setattr(provider, "react_to_outcome", blocking_reaction)
    monkeypatch.setattr(gitlab_webhook, "get_git_provider_with_context", lambda pr_url: provider)
    monkeypatch.setattr(gitlab_webhook, "is_bot_user", lambda data: False)
    monkeypatch.setattr(gitlab_webhook, "authenticate_gitlab_webhook", lambda *args: None)
    monkeypatch.setattr(gitlab_webhook, "handle_request", handle_request)
    background = BackgroundTasks()
    # The webhook authenticates first and only then reads and parses the body.
    request = SimpleNamespace(
        headers={},
        body=AsyncMock(return_value=json.dumps(_note_event()).encode()),
    )

    with request_cycle_context({}):
        await gitlab_webhook.gitlab_webhook(background, request)
        # Override the request-scoped copy installed by the webhook.
        monkeypatch.setattr(get_settings().config, "reaction_on_success", "hooray", raising=False)
        observer = asyncio.create_task(release_reaction())
        try:
            await background()
        finally:
            released.set()
            await observer

    assert provider.reactions == [(COMMENT_ID, "eyes"), (COMMENT_ID, "hooray")]
    assert provider.removed == [(COMMENT_ID, 1)]


def test_handle_request_hands_the_verdict_back_to_its_caller(monkeypatch):
    """The reaction is only as honest as this return value, so pin the value itself."""
    import pr_agent.servers.gitlab_webhook as gitlab_webhook

    class _Agent:
        async def handle_request(self, api_url, body, notify=None):
            return False

    monkeypatch.setattr(gitlab_webhook, "PRAgent", lambda: _Agent())

    # sender_id is None, so the command-actor lookup above is skipped rather than taken: this test
    # is about the return value surviving the dispatcher, not about who the command is attributed to
    result = asyncio.run(gitlab_webhook.handle_request(
        "https://gitlab.example.com/mr/1", "/review", {}, None))

    assert result is False


def test_an_ordinary_comment_gets_no_reaction_at_all(run_comment):
    """Every comment on a merge request arrives here, and only a command is work to report on."""
    provider = run_comment(success="hooray", failure="confused", body="looks good to me")

    assert provider.reactions == [], "an ordinary comment is neither a success nor a failure"
    assert provider.removed == []


def test_an_unknown_command_gets_no_reaction(run_comment):
    provider = run_comment(success="hooray", failure="confused", body="/not_a_command")

    assert provider.reactions == []


def test_a_command_the_dispatcher_rewrote_still_gets_its_outcome(run_comment):
    """`/ask` on a diff line is dispatched as `/ask_line`, so a gate reading the raw first word
    would skip the outcome and leave the start reaction on the comment forever."""
    provider = run_comment(success="hooray", payload=_diff_note_event("/ask what does this do?"))

    assert provider.dispatched[0][0] == "/ask_line", "the rewrite has to have run"
    assert provider.reactions == [(COMMENT_ID, "eyes"), (COMMENT_ID, "hooray")]
    assert provider.removed == [(COMMENT_ID, 1)]


def test_a_command_with_quoted_arguments_gets_its_outcome(run_comment):
    """The dispatcher reads a comment with shlex, so a quoted first token is still a command."""
    provider = run_comment(success="hooray", body='/review "extra instructions"')

    assert provider.reactions == [(COMMENT_ID, "eyes"), (COMMENT_ID, "hooray")]
    assert provider.removed == [(COMMENT_ID, 1)]


def test_auto_review_from_a_comment_gets_no_reaction(run_comment):
    """`auto_review` never acknowledges, so there is no start reaction for an outcome to replace.

    It runs on every merge request rather than on somebody asking for something, and
    `tests/unittest/test_pr_agent_routing.py` pins that it stays silent. Nothing is orphaned
    here because the start reaction is missing too.
    """
    provider = run_comment(success="hooray", body="/auto_review")

    assert provider.reactions == []
    assert provider.removed == []


@pytest.mark.parametrize("command", ["/review", "/describe", "/improve"])  # one per dispatch shape
def test_the_dispatcher_acknowledges_every_command_it_runs(monkeypatch, command):
    """`notify` is how a caller learns a command was dispatched, which is the webhook's gate.

    `auto_review` is the exception and stays that way: it runs on every merge request rather than
    on somebody asking for something, and its silence is pinned in test_pr_agent_routing.py.
    """

    class _Tool:
        def __init__(self, pr_url, ai_handler=None, args=None, **kwargs):
            self.pr_url = pr_url

        async def run(self):
            return True

    acknowledged = []
    monkeypatch.setattr(pr_agent_module, "PRReviewer", _Tool)  # `answer` builds one directly
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context",
                        lambda pr_url: _RecordingProvider())
    monkeypatch.setitem(pr_agent_module.command2class, command.lstrip("/"), _Tool)

    result = asyncio.run(pr_agent_module.PRAgent(ai_handler="fake-ai").handle_request(
        "https://github.com/org/repo/pull/1", command, notify=lambda: acknowledged.append(command)))

    assert result is True
    assert acknowledged == [command], f"{command} ran without acknowledging"


def test_auto_review_never_acknowledges_on_the_real_dispatcher(monkeypatch):
    """The negative half of the gate, on the real dispatcher rather than a stand-in for it.

    The three "no reaction" tests above run through `run_comment`, whose fake `handle_request`
    decides what to notify on its own, so they would stay green even if the dispatcher started
    acknowledging `auto_review`. This one goes through `PRAgent`.
    """

    class _Tool:
        def __init__(self, pr_url, ai_handler=None, args=None, **kwargs):
            self.pr_url = pr_url

        async def run(self):
            return True

    acknowledged = []
    monkeypatch.setattr(pr_agent_module, "PRReviewer", _Tool)
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context",
                        lambda pr_url: _RecordingProvider())

    result = asyncio.run(pr_agent_module.PRAgent(ai_handler="fake-ai").handle_request(
        "https://github.com/org/repo/pull/1", "/auto_review", notify=lambda: acknowledged.append("auto")))

    assert result is True, "auto_review runs, it just does not acknowledge"
    assert acknowledged == [], "the webhook gates on this, so a notification here means a stray reaction"
