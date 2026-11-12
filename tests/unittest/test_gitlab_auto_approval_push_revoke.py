"""A GitLab push dismisses PR-Agent's approval before any filter can skip the event (#4698)."""

from unittest.mock import MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.servers import gitlab_webhook

MR_URL = "https://gitlab.com/org/repo/-/merge_requests/1"


def _payload(action="update", oldrev="abc123", **extra):
    return {
        "object_kind": "merge_request",
        "user": {"name": "developer", "id": 2},
        "object_attributes": {"action": action, "oldrev": oldrev, "url": MR_URL},
        **extra,
    }


@pytest.fixture
def provider(monkeypatch):
    provider = MagicMock()
    monkeypatch.setattr(gitlab_webhook, "get_git_provider_with_context", lambda url: provider)
    return provider


def test_helper_does_nothing_when_auto_approval_is_off(monkeypatch):
    monkeypatch.setitem(get_settings().config, "enable_auto_approval", False)
    factory = MagicMock()
    monkeypatch.setattr(gitlab_webhook, "get_git_provider_with_context", factory)

    gitlab_webhook.revoke_auto_approval_on_push(_payload())

    factory.assert_not_called()


@pytest.mark.parametrize("flag", [True, "true"])
def test_helper_dismisses_on_push(monkeypatch, provider, flag):
    monkeypatch.setitem(get_settings().config, "enable_auto_approval", flag)

    gitlab_webhook.revoke_auto_approval_on_push(_payload())

    provider.revoke_auto_approval.assert_called_once()
    assert "new commits were pushed" in provider.revoke_auto_approval.call_args.args[0]


def test_helper_reads_the_gate_from_the_host_layer(monkeypatch, provider):
    monkeypatch.setitem(get_settings().config, "enable_auto_approval", False)
    monkeypatch.setattr(
        gitlab_webhook, "apply_host_settings",
        lambda: get_settings().config.update({"enable_auto_approval": True}),
    )

    gitlab_webhook.revoke_auto_approval_on_push(_payload())

    provider.revoke_auto_approval.assert_called_once()


@pytest.mark.parametrize("payload", [
    _payload(action="update", oldrev=None),
    _payload(action="open"),
    _payload(action="reopen"),
    {"object_kind": "note", "object_attributes": {"action": "update", "oldrev": "abc"}},
])
def test_helper_ignores_events_that_keep_the_code(monkeypatch, provider, payload):
    monkeypatch.setitem(get_settings().config, "enable_auto_approval", True)

    gitlab_webhook.revoke_auto_approval_on_push(payload)

    provider.revoke_auto_approval.assert_not_called()


def test_helper_writes_nothing_on_a_dry_run(monkeypatch, provider):
    monkeypatch.setitem(get_settings().config, "enable_auto_approval", True)
    monkeypatch.setitem(get_settings().config, "publish_output", False)

    gitlab_webhook.revoke_auto_approval_on_push(_payload())

    provider.revoke_auto_approval.assert_not_called()


def test_helper_logs_provider_errors(monkeypatch):
    monkeypatch.setitem(get_settings().config, "enable_auto_approval", True)

    def boom(url):
        raise RuntimeError("no token")

    monkeypatch.setattr(gitlab_webhook, "get_git_provider_with_context", boom)

    gitlab_webhook.revoke_auto_approval_on_push(_payload())


@pytest.mark.asyncio
async def test_event_revokes_before_the_bot_sender_filter(monkeypatch):
    calls = []
    monkeypatch.setattr(gitlab_webhook, "revoke_auto_approval_on_push", calls.append)
    monkeypatch.setattr(gitlab_webhook, "is_bot_user", lambda data: True)
    body = _payload(user={"name": "renovate[bot]", "id": 1})

    await gitlab_webhook.process_gitlab_event(body, {})

    assert calls == [body]
