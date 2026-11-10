"""A push or a base-branch change dismisses PR-Agent's approval before any filter can skip the event (#4698)."""

import json
from unittest.mock import MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.servers import github_action_runner, github_app, github_common

PR_URL = "https://api.github.com/repos/org/repo/pulls/1"


def _payload(action, **extra):
    return {"action": action, "pull_request": {"url": PR_URL}, **extra}


@pytest.fixture
def provider(monkeypatch):
    provider = MagicMock()
    monkeypatch.setattr(github_common, "get_git_provider_with_context", lambda url: provider)
    return provider


def test_helper_does_nothing_when_auto_approval_is_off(monkeypatch):
    monkeypatch.setitem(get_settings().config, "enable_auto_approval", False)
    factory = MagicMock()
    monkeypatch.setattr(github_common, "get_git_provider_with_context", factory)

    github_common.revoke_auto_approval_on_push(_payload("synchronize"))

    factory.assert_not_called()


@pytest.mark.parametrize("flag", [True, "true"])
def test_helper_dismisses_on_push(monkeypatch, provider, flag):
    monkeypatch.setitem(get_settings().config, "enable_auto_approval", flag)

    github_common.revoke_auto_approval_on_push(_payload("synchronize"))

    provider.revoke_auto_approval.assert_called_once()
    assert "new commits were pushed" in provider.revoke_auto_approval.call_args.args[0]


def test_helper_dismisses_when_the_base_branch_changes(monkeypatch, provider):
    monkeypatch.setitem(get_settings().config, "enable_auto_approval", True)

    github_common.revoke_auto_approval_on_push(_payload("edited", changes={"base": {"ref": {"from": "main"}}}))

    assert "the base branch changed" in provider.revoke_auto_approval.call_args.args[0]


@pytest.mark.parametrize("payload", [
    _payload("edited", changes={"title": {"from": "old"}}),
    _payload("opened"),
    _payload("labeled"),
])
def test_helper_ignores_events_that_keep_the_code(monkeypatch, provider, payload):
    monkeypatch.setitem(get_settings().config, "enable_auto_approval", True)

    github_common.revoke_auto_approval_on_push(payload)

    provider.revoke_auto_approval.assert_not_called()


def test_helper_logs_provider_errors(monkeypatch):
    monkeypatch.setitem(get_settings().config, "enable_auto_approval", True)

    def boom(url):
        raise RuntimeError("no installation")

    monkeypatch.setattr(github_common, "get_git_provider_with_context", boom)

    github_common.revoke_auto_approval_on_push(_payload("synchronize"))


@pytest.mark.asyncio
async def test_app_revokes_before_the_bot_sender_filter(monkeypatch):
    calls = []
    monkeypatch.setattr(github_app, "revoke_auto_approval_on_push", calls.append)
    body = {
        "action": "synchronize",
        "sender": {"login": "renovate[bot]", "type": "Bot", "id": 1},
        "pull_request": {"url": PR_URL, "html_url": "https://github.com/org/repo/pull/1"},
        "repository": {"full_name": "org/repo"},
    }

    assert await github_app._dispatch_request(body, "pull_request", "synchronize") == {}
    assert calls == [body]


@pytest.mark.asyncio
async def test_app_only_hands_pull_request_events_to_the_helper(monkeypatch):
    calls = []
    monkeypatch.setattr(github_app, "revoke_auto_approval_on_push", calls.append)
    body = {"action": "submitted", "sender": {"login": "renovate[bot]", "type": "Bot", "id": 1}}

    await github_app._dispatch_request(body, "pull_request_review", "submitted")

    assert calls == []


@pytest.mark.asyncio
async def test_action_revokes_before_repo_settings_and_push_filters(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(github_action_runner, "revoke_auto_approval_on_push", calls.append)

    def no_repo_settings(pr_url):
        assert calls, "revocation must run before repository settings are loaded"

    monkeypatch.setattr(github_action_runner, "apply_repo_settings", no_repo_settings)
    payload = {
        "action": "synchronize",
        "before": "abc",
        "after": "def",
        "sender": {"type": "Bot"},
        "pull_request": {"url": PR_URL, "html_url": "https://github.com/org/repo/pull/1"},
    }
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    try:
        await github_action_runner.run_action()
    except SystemExit:
        pass

    assert calls == [payload]
