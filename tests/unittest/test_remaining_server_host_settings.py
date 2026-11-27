"""Fail closed on unavailable Host settings for Azure DevOps and Bitbucket Server webhooks."""

import copy
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import BackgroundTasks, FastAPI
from starlette.middleware import Middleware
from starlette.testclient import TestClient
from starlette_context import context, request_cycle_context
from starlette_context.middleware import RawContextMiddleware

from pr_agent.config_loader import get_settings, global_settings
from pr_agent.servers import azuredevops_server_webhook, bitbucket_server_webhook
from pr_agent.servers.utils import preload_host_settings_for_new_automatic_event

PR_URL = "https://git.example.test/org/repo/pulls/1"


@pytest.fixture(params=["missing", "available"])
def host_settings(request, tmp_path):
    source = tmp_path / "host.toml"
    if request.param == "available":
        source.write_text(
            '[config]\nignore_pr_title = ["^Blocked"]\n'
            '[litellm]\nturn_off_message_logging = true\n'
        )
    settings = copy.deepcopy(global_settings)
    settings.set("CONFIG.EXTRA_CONFIG_URL", str(source))
    return settings, request.param == "missing"


@pytest.mark.asyncio
async def test_server_preload_keeps_host_logging_opt_out_before_repo_override(tmp_path, monkeypatch):
    from pr_agent.git_providers import utils as provider_utils

    source = tmp_path / "host.toml"
    source.write_text('[litellm]\nturn_off_message_logging = true\n')
    settings = copy.deepcopy(global_settings)
    settings.set("CONFIG.EXTRA_CONFIG_URL", str(source))
    settings.set("CONFIG.USE_REPO_SETTINGS_FILE", True)

    class Provider:
        def get_repo_settings(self):
            return b'[litellm]\nturn_off_message_logging = false\n'

    monkeypatch.setattr(provider_utils, "get_git_provider_with_context", lambda _url: Provider())
    with request_cycle_context({"settings": settings}):
        assert await preload_host_settings_for_new_automatic_event() is True
        provider_utils.apply_repo_settings(PR_URL)
        assert get_settings().get("LITELLM.TURN_OFF_MESSAGE_LOGGING") is False
        assert context.get("_pr_agent_host_message_logging_disabled") is True


@pytest.mark.asyncio
async def test_server_preload_rejects_non_string_host_source():
    settings = copy.deepcopy(global_settings)
    settings.set("CONFIG.EXTRA_CONFIG_URL", 42)
    with request_cycle_context({"settings": settings}):
        assert await preload_host_settings_for_new_automatic_event() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, "", "  ", False])
async def test_server_preload_treats_disabled_host_source_as_unset(source):
    settings = copy.deepcopy(global_settings)
    settings.set("CONFIG.EXTRA_CONFIG_URL", source)
    with request_cycle_context({"settings": settings}):
        assert await preload_host_settings_for_new_automatic_event() is True


@pytest.mark.asyncio
async def test_azure_automatic_event_is_skipped_when_host_settings_fail(monkeypatch, host_settings):
    settings, failed = host_settings
    handler = AsyncMock()
    agent = Mock(handle_request=handler)

    def repo_settings(_url):
        assert context.get("_pr_agent_host_message_logging_disabled") is True
        assert get_settings().get("CONFIG.IGNORE_PR_TITLE") == ["^Blocked"]

    repo_apply = Mock(side_effect=repo_settings)
    monkeypatch.setattr(azuredevops_server_webhook, "apply_repo_settings", repo_apply)
    monkeypatch.setattr(azuredevops_server_webhook, "auto_feedback_disabled", lambda: False)
    monkeypatch.setattr(azuredevops_server_webhook, "get_pr_commands", lambda _: ["/review"])
    monkeypatch.setattr(azuredevops_server_webhook, "prepare_command", lambda command: command)

    with request_cycle_context({"settings": settings}):
        await azuredevops_server_webhook._perform_commands_azure("pr_commands", agent, PR_URL, {})
        if not failed:
            assert context.get("_pr_agent_host_message_logging_disabled") is True

    assert repo_apply.call_count == (0 if failed else 1)
    assert handler.await_count == (0 if failed else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("event_key", ["pr:opened", "pr:comment:added"])
async def test_bitbucket_server_only_skips_automatic_events_on_host_failure(monkeypatch, host_settings, event_key):
    settings, failed = host_settings
    manual = event_key == "pr:comment:added"
    settings.set("BITBUCKET_SERVER.WEBHOOK_SECRET", "test-secret")
    settings.set("BITBUCKET_SERVER.URL", "https://bitbucket.example.test")
    monkeypatch.setattr(bitbucket_server_webhook, "global_settings", settings)
    monkeypatch.setattr(bitbucket_server_webhook, "verify_signature", lambda *_args: None)

    def repo_settings(_url):
        assert context.get("_pr_agent_host_message_logging_disabled") is True
        assert get_settings().get("CONFIG.IGNORE_PR_TITLE") == ["^Blocked"]

    repo_apply = Mock(side_effect=repo_settings)
    monkeypatch.setattr(bitbucket_server_webhook, "apply_repo_settings", repo_apply)
    monkeypatch.setattr(bitbucket_server_webhook, "should_process_pr_logic", lambda _data: True)
    monkeypatch.setattr(bitbucket_server_webhook, "auto_feedback_disabled", lambda: False)
    monkeypatch.setattr(bitbucket_server_webhook, "get_pr_commands", lambda _: ["/review"])
    dispatched = AsyncMock()
    monkeypatch.setattr(bitbucket_server_webhook, "_run_commands_sequentially", dispatched)
    payload = {
        "eventKey": event_key,
        "comment": {"text": "/review"},
        "pullRequest": {
            "id": 7,
            "toRef": {"repository": {"slug": "repo", "project": {"key": "PROJ"}}},
        },
    }

    class Request:
        headers = {"x-hub-signature": "test-signature"}

        async def body(self):
            return b'{}'

        async def json(self):
            return payload

    tasks = BackgroundTasks()
    with request_cycle_context({"settings": settings}):
        response = await bitbucket_server_webhook.handle_webhook(tasks, Request())
        await tasks()
        if manual:
            assert not context.get("automatic_host_settings_source")
        elif not failed:
            assert context.get("_pr_agent_host_message_logging_disabled") is True

    assert response.status_code == 200
    assert repo_apply.call_count == (0 if manual or failed else 1)
    assert dispatched.await_count == (1 if manual or not failed else 0)
    if manual:
        assert dispatched.await_args.args[0] == ["/review"]
        assert dispatched.await_args.args[2]["event"] == "comment"


def test_azure_background_preload_preserves_request_context(monkeypatch, host_settings):
    settings, failed = host_settings
    monkeypatch.setattr(azuredevops_server_webhook, "global_settings", settings)
    monkeypatch.setattr(azuredevops_server_webhook, "WEBHOOK_USERNAME", "test-user")
    monkeypatch.setattr(azuredevops_server_webhook, "WEBHOOK_PASSWORD", "test-pass")
    seen = []

    async def handle_background(_data, _log_context):
        seen.append((context.exists(), await preload_host_settings_for_new_automatic_event()))

    monkeypatch.setattr(azuredevops_server_webhook, "handle_request_azure", handle_background)
    app = FastAPI(middleware=[Middleware(RawContextMiddleware)])
    app.include_router(azuredevops_server_webhook.router)
    with TestClient(app) as client:
        response = client.post("/", json={"eventType": "git.pullrequest.created"},
                               auth=("test-user", "test-pass"))

    assert response.status_code == 202
    assert seen == [(True, not failed)]
