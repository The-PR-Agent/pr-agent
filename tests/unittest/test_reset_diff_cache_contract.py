"""The base-class diff-cache reset every provider inherits (#4372).

#3724 gave GitHub an unconditional reset; the contract lives in GitProvider now, so
any provider's auto-command boundary drops the whole diff cache — non-empty ones
included — and the incremental anchor, plus the request-scoped diff cache. The
non-GitHub server loops call it between every auto command; GitHub's loop is pinned
in test_github_empty_diff_cache.py.
"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

from starlette_context import context, request_cycle_context

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider, IncrementalPR
from pr_agent.servers import (
    azuredevops_server_webhook,
    bitbucket_app,
    bitbucket_server_webhook,
    gitea_app,
    gitlab_webhook,
)

API_URL = "https://git.example/api/v4/repos/org/repo/pulls/1"
COMMANDS = ["/describe", "/review"]


def _provider_with_state():
    provider = SimpleNamespace(
        diff_files=[MagicMock()],
        incremental=IncrementalPR(True),
    )
    return provider


def test_reset_drops_non_empty_cache_and_incremental_anchor():
    provider = _provider_with_state()

    GitProvider.reset_diff_cache_for_command(provider)

    assert provider.diff_files is None
    assert provider.incremental.is_incremental is False


def test_reset_pops_the_request_scoped_diff_cache():
    provider = _provider_with_state()

    with request_cycle_context({"diff_files": provider.diff_files}):
        GitProvider.reset_diff_cache_for_command(provider)
        assert context.get("diff_files", "gone") == "gone"


def test_reset_is_safe_outside_a_request_and_without_diff_state():
    provider = SimpleNamespace()

    # No request context and no diff/incremental attributes: still a clean no-op.
    GitProvider.reset_diff_cache_for_command(provider)

    assert provider.diff_files is None
    assert getattr(provider, "incremental", None) is None


def _loop_provider_and_agent(monkeypatch, server_module):
    reset = Mock()
    provider = SimpleNamespace(reset_diff_cache_for_command=reset)
    agent = SimpleNamespace(handle_request=AsyncMock(return_value=True))
    monkeypatch.setattr(server_module, "prepare_command", lambda command: command)
    return reset, provider, agent


def _loop_context(provider):
    settings = deepcopy(get_settings())
    settings.set("config.disable_auto_feedback", False)
    return {"settings": settings, "git_provider": {API_URL: provider}}


async def test_gitlab_loop_resets_between_auto_commands(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, gitlab_webhook)
    monkeypatch.setattr(gitlab_webhook, "get_pr_commands", lambda _name: COMMANDS)
    monkeypatch.setattr(gitlab_webhook, "should_process_pr_logic", lambda _data: True)

    with request_cycle_context(_loop_context(provider)):
        await gitlab_webhook._perform_commands_gitlab("pr_commands", agent, API_URL, {}, {})

    assert reset.call_count == len(COMMANDS)
    assert agent.handle_request.await_count == len(COMMANDS)


async def test_gitea_loop_resets_between_auto_commands(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, gitea_app)
    monkeypatch.setattr(gitea_app, "get_pr_commands", lambda _name: COMMANDS)

    with request_cycle_context(_loop_context(provider)):
        await gitea_app._perform_commands_gitea("pr_commands", agent, {}, API_URL)

    assert reset.call_count == len(COMMANDS)
    assert agent.handle_request.await_count == len(COMMANDS)


async def test_azure_loop_resets_between_auto_commands(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, azuredevops_server_webhook)
    monkeypatch.setattr(azuredevops_server_webhook, "get_pr_commands", lambda _name: COMMANDS)
    monkeypatch.setattr(azuredevops_server_webhook, "apply_repo_settings", lambda _url: None)

    with request_cycle_context(_loop_context(provider)):
        await azuredevops_server_webhook._perform_commands_azure("pr_commands", agent, API_URL, {})

    assert reset.call_count == len(COMMANDS)
    assert agent.handle_request.await_count == len(COMMANDS)


async def test_bitbucket_loop_resets_between_auto_commands(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, bitbucket_app)

    with request_cycle_context(_loop_context(provider)):
        await bitbucket_app._run_commands_bitbucket(COMMANDS, agent, API_URL, {})

    assert reset.call_count == len(COMMANDS)
    assert agent.handle_request.await_count == len(COMMANDS)


async def test_bitbucket_server_loop_resets_between_auto_commands(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, bitbucket_server_webhook)
    monkeypatch.setattr(bitbucket_server_webhook, "_process_command", lambda command, _url: command)
    monkeypatch.setattr(bitbucket_server_webhook, "PRAgent", lambda: agent)

    with request_cycle_context(_loop_context(provider)):
        await bitbucket_server_webhook._run_commands_sequentially(COMMANDS, API_URL, {})

    assert reset.call_count == len(COMMANDS)
    assert agent.handle_request.await_count == len(COMMANDS)
