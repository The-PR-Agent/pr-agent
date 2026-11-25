"""An automatic webhook command must not run when the host settings cannot be loaded."""

import copy
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import BackgroundTasks
from starlette_context import request_cycle_context

from pr_agent.config_loader import get_settings, global_settings
from pr_agent.servers import bitbucket_app, gitea_app, github_app, gitlab_webhook
from pr_agent.servers.utils import host_settings_unavailable, load_host_settings_for_webhook

PR_URL = "https://git.example.test/org/repo/pulls/1"


@pytest.fixture(params=["missing", "available", "disabled", "malformed", "forbidden", "scalar", "non_string"])
def host_settings(request, tmp_path):
    """Return a settings object whose host source cannot, or can, be loaded."""
    source = tmp_path / "host.toml"
    if request.param == "available":
        source.write_text('[config]\nignore_pr_title = ["^Blocked"]\n')
    elif request.param == "malformed":
        source.write_text("[config\n")
    elif request.param == "forbidden":
        source.write_text('[config]\npreload = ["untrusted.toml"]\n')
    elif request.param == "scalar":
        source.write_text('ignore_pr_title = ["^Blocked"]\n')
    settings = copy.deepcopy(global_settings)
    source_value = {"non_string": 42, "disabled": False}.get(request.param, str(source))
    settings.set("CONFIG.EXTRA_CONFIG_URL", source_value)
    return settings, request.param not in ("available", "disabled")


@pytest.mark.asyncio
async def test_host_settings_unavailable_reports_only_a_failed_load(tmp_path):
    good = tmp_path / "good.toml"
    good.write_text("[config]\n")
    settings = copy.deepcopy(global_settings)

    with request_cycle_context({"settings": settings}):
        assert host_settings_unavailable() is False  # no source pinned yet

    settings.set("CONFIG.EXTRA_CONFIG_URL", str(good))
    with request_cycle_context({"settings": settings}):
        assert host_settings_unavailable() is False  # nothing loaded yet
        await load_host_settings_for_webhook()
        assert host_settings_unavailable() is False

    settings.set("CONFIG.EXTRA_CONFIG_URL", str(tmp_path / "missing.toml"))
    with request_cycle_context({"settings": settings}):
        await load_host_settings_for_webhook()
        assert host_settings_unavailable() is True

    assert host_settings_unavailable() is False  # outside a request


@pytest.mark.asyncio
async def test_github_automatic_event_is_skipped_when_host_settings_fail(monkeypatch, host_settings):
    settings, failed = host_settings
    opened = AsyncMock(return_value={})
    monkeypatch.setattr(github_app, "handle_new_pr_opened", opened)
    monkeypatch.setattr(github_app, "should_process_pr_logic", lambda _body: True)
    body = {
        "action": "opened", "sender": {"login": "alice", "type": "User", "id": 1},
        "repository": {"full_name": "org/repo"},
    }

    with request_cycle_context({"settings": settings}):
        await github_app._dispatch_request(body, "pull_request", "opened")

    assert opened.await_count == (0 if failed else 1)


@pytest.mark.asyncio
async def test_gitlab_automatic_event_is_skipped_when_host_settings_fail(monkeypatch, host_settings):
    settings, failed = host_settings
    perform = AsyncMock()
    monkeypatch.setattr(gitlab_webhook, "_perform_commands_gitlab", perform)
    monkeypatch.setattr(gitlab_webhook, "apply_new_mr_repo_settings", AsyncMock())
    monkeypatch.setattr(gitlab_webhook, "revoke_auto_approval_on_push", Mock())
    monkeypatch.setattr(gitlab_webhook, "should_process_pr_logic", lambda _data: True)
    data = {
        "object_kind": "merge_request",
        "object_attributes": {"action": "open", "url": PR_URL, "title": "Regular MR"},
        "user": {"username": "alice", "id": 1},
        "project": {"path_with_namespace": "org/repo"},
    }

    with request_cycle_context({"settings": settings}):
        await gitlab_webhook.process_gitlab_event(data, {})

    assert perform.await_count == (0 if failed else 1)


@pytest.mark.asyncio
async def test_gitea_automatic_event_is_skipped_when_host_settings_fail(monkeypatch, host_settings):
    settings, failed = host_settings
    handled = AsyncMock()
    monkeypatch.setattr(gitea_app, "PRAgent", lambda: object())
    monkeypatch.setattr(gitea_app, "handle_pr_event", handled)
    monkeypatch.setattr(gitea_app, "should_process_pr_logic", lambda _body: True)

    with request_cycle_context({"settings": settings}):
        await gitea_app.handle_request({"action": "opened", "pull_request": {"url": PR_URL}}, "pull_request")

    assert handled.await_count == (0 if failed else 1)


@pytest.mark.asyncio
async def test_bitbucket_automatic_event_is_skipped_when_host_settings_fail(monkeypatch, host_settings):
    settings, failed = host_settings
    monkeypatch.setattr(bitbucket_app, "global_settings", settings)
    monkeypatch.setattr(bitbucket_app, "_verify_webhook_jwt", lambda *_args: ("client", "secret"))
    oauth = AsyncMock(return_value="token")
    monkeypatch.setattr(bitbucket_app, "get_bearer_token", oauth)
    monkeypatch.setattr(bitbucket_app, "should_process_pr_logic", lambda _data: True)
    payload = {
        "event": "pullrequest:created",
        "data": {
            "actor": {"type": "user", "display_name": "alice"},
            "pullrequest": {
                "title": "Regular PR", "author": {"username": "alice"},
                "links": {"html": {"href": PR_URL}},
                "source": {"branch": {"name": "feature"}},
                "destination": {"branch": {"name": "main"}, "repository": {"full_name": "org/repo"}},
            },
        },
    }

    class Request:
        headers = {"authorization": "JWT test-token"}

        async def json(self):
            return payload

    tasks = BackgroundTasks()
    with request_cycle_context({"settings": copy.deepcopy(settings)}):
        assert await bitbucket_app.handle_github_webhooks(tasks, Request()) == "OK"
        await tasks()

    assert oauth.await_count == (0 if failed else 1)


@pytest.mark.asyncio
async def test_frozen_host_settings_fail_if_env_overrides_cannot_be_restored(monkeypatch, tmp_path):
    from pr_agent.git_providers import utils as git_utils

    host = tmp_path / "host.toml"
    host.write_text("[config]\nenable_auto_approval = false\npublish_output = false\n")
    settings = copy.deepcopy(global_settings)
    settings.set("CONFIG.EXTRA_CONFIG_URL", str(host))
    settings.set("CONFIG.ENABLE_AUTO_APPROVAL", True)
    settings.set("CONFIG.PUBLISH_OUTPUT", True)
    monkeypatch.setattr(git_utils.env_loader, "load", Mock(side_effect=RuntimeError("unavailable")))

    with request_cycle_context({"settings": settings}):
        await load_host_settings_for_webhook()
        assert host_settings_unavailable() is True
        assert get_settings().get("CONFIG.ENABLE_AUTO_APPROVAL") is True
        assert get_settings().get("CONFIG.PUBLISH_OUTPUT") is True


@pytest.mark.asyncio
async def test_invalid_frozen_host_settings_do_not_apply_preceding_inline_table(tmp_path):
    host = tmp_path / "host.toml"
    host.write_text('config = {ignore_pr_title = ["^Blocked"]}\ninvalid = 42\n')
    settings = copy.deepcopy(global_settings)
    settings.set("CONFIG.EXTRA_CONFIG_URL", str(host))
    settings.set("CONFIG.IGNORE_PR_TITLE", [])

    with request_cycle_context({"settings": settings}):
        await load_host_settings_for_webhook()
        assert host_settings_unavailable() is True
        assert get_settings().get("CONFIG.IGNORE_PR_TITLE") == []


@pytest.mark.asyncio
async def test_automatic_command_is_skipped_when_frozen_host_replay_fails(monkeypatch, tmp_path):
    from pr_agent.agent import pr_agent as agent
    from pr_agent.agent.request_policy import RequestOutcome
    from pr_agent.git_providers import utils as git_utils

    host = tmp_path / "host.toml"
    host.write_text("[config]\n")
    settings = copy.deepcopy(global_settings)
    settings.set("CONFIG.EXTRA_CONFIG_URL", str(host))
    settings.set("CONFIG.USE_REPO_SETTINGS_FILE", False)
    settings.set("CONFIG.IS_AUTO_COMMAND", True)
    monkeypatch.setattr(git_utils, "get_git_provider_with_context", lambda _url: Mock())
    notify, factory = Mock(), Mock()
    monkeypatch.setitem(agent.command2class, "review", factory)
    monkeypatch.setattr(agent, "flush_telemetry", Mock())

    with request_cycle_context({"settings": settings}):
        await load_host_settings_for_webhook()
        assert host_settings_unavailable() is False
        monkeypatch.setattr(git_utils.env_loader, "load", Mock(side_effect=RuntimeError("unavailable")))
        assert await agent.PRAgent().handle_request(PR_URL, "/review", notify=notify) is RequestOutcome.SKIPPED
        assert host_settings_unavailable() is True

    factory.assert_not_called()
    notify.assert_not_called()
