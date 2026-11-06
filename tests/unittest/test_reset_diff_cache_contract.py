"""The base-class diff-cache reset every provider inherits (#4372).

#3724 gave GitHub an unconditional reset; the contract lives in GitProvider now, so
any provider's auto-command boundary drops the whole diff cache — non-empty ones
included — and the incremental anchor, plus the request-scoped diff cache. The
non-GitHub server loops call it between every auto command; GitHub's loop is pinned
in test_github_empty_diff_cache.py.
"""

import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from giteapy.rest import ApiException
from gitlab import GitlabGetError
from starlette_context import context, request_cycle_context

from pr_agent.agent.request_policy import RequestOutcome
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider, IncrementalPR
from pr_agent.git_providers.gitea_provider import GiteaProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider
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
        _latest_commit_url="https://git.example/commit/previous",
    )
    return provider


def test_reset_drops_non_empty_cache_and_incremental_anchor():
    provider = _provider_with_state()

    GitProvider.reset_diff_cache_for_command(provider)

    assert provider.diff_files is None
    assert provider.incremental.is_incremental is False
    assert provider._latest_commit_url is None


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


@pytest.mark.parametrize("is_pull_request", [False, True])
def test_gitea_tool_reuse_requires_a_verified_pr_refresh(monkeypatch, is_pull_request):
    from pr_agent.git_providers import get_git_provider_for_tool

    provider = GiteaProvider.__new__(GiteaProvider)
    snapshot = SimpleNamespace(
        head=SimpleNamespace(sha="head"), base=SimpleNamespace(sha="base", ref="main")
    )
    provider.__dict__.update(
        enabled_pr=is_pull_request, pr=snapshot, owner="owner", repo="repo", pr_number=1,
        sha="head", base_sha="base", base_ref="main", logger=Mock(),
        repo_api=SimpleNamespace(get_pull_request=Mock(return_value=snapshot)),
        _tool_provider_stale=False,
    )
    url = f"https://gitea.example/owner/repo/{'pulls' if is_pull_request else 'issues'}/1"
    settings = deepcopy(get_settings())
    settings.set("config.git_provider", "gitea")
    settings.set("config.extra_config_url", "")
    with request_cycle_context({"settings": settings, "git_provider": {url: provider}}):
        provider.reset_diff_cache_for_command()
        assert provider._tool_provider_stale is not is_pull_request
        if is_pull_request:
            assert get_git_provider_for_tool(url) is provider
        else:
            monkeypatch.setattr(GiteaProvider, "__init__", lambda self, _url: None)
            assert get_git_provider_for_tool(url) is not provider
            provider.repo_api.get_pull_request.assert_not_called()


def test_gitlab_tool_reuse_uses_refreshed_mr_metadata():
    from pr_agent.git_providers import get_git_provider_for_tool

    provider = GitLabProvider.__new__(GitLabProvider)
    before = SimpleNamespace(
        title="Original", source_branch="feature", target_branch="main",
        diff_refs={"head_sha": "old-head"},
    )
    after = SimpleNamespace(
        title="Updated", source_branch="feature", target_branch="main",
        diff_refs={"head_sha": "new-head"},
    )
    provider.mr = before
    provider.id_project = "org/repo"
    provider.id_mr = 1
    provider._get_merge_request = Mock(return_value=after)
    provider._incremental_notes_cache = ["old-note"]
    provider.mr_commits = ["old-commit"]
    settings = deepcopy(get_settings())
    settings.set("config.git_provider", "gitlab")
    settings.set("config.use_repo_settings_file", False)
    settings.set("config.extra_config_url", "")
    url = "https://gitlab.example/org/repo/-/merge_requests/1"
    with request_cycle_context({"settings": settings, "git_provider": {url: provider}}):
        provider.reset_diff_cache_for_command()
        assert get_git_provider_for_tool(url) is provider
        assert provider.get_pr_head_sha() == "new-head"
        assert provider.get_request_policy_metadata({"title"})["title"] == "Updated"
        assert provider.mr_commits is None
        assert not hasattr(provider, "_incremental_notes_cache")


@pytest.mark.parametrize("retargeted", [False, True])
def test_gitlab_command_boundary_never_reuses_an_unverified_mr(retargeted):
    provider = GitLabProvider.__new__(GitLabProvider)
    original = SimpleNamespace(target_branch="main")
    provider.mr = original
    if retargeted:
        provider._get_merge_request = Mock(
            return_value=SimpleNamespace(target_branch="new-target")
        )
    else:
        provider._get_merge_request = Mock(side_effect=GitlabGetError("unavailable"))
    with pytest.raises((ValueError, GitlabGetError)):
        provider.reset_diff_cache_for_command()
    assert provider._tool_provider_stale is True
    assert provider.mr is original


def _loop_provider_and_agent(monkeypatch, server_module):
    provider = _provider_with_state()
    reset = Mock(side_effect=lambda: GitProvider.reset_diff_cache_for_command(provider))
    provider.reset_diff_cache_for_command = reset
    provider.command_urls = []

    async def handle_request(*_args):
        provider.command_urls.append(provider._latest_commit_url)
        provider._latest_commit_url = "https://git.example/commit/current"
        return True

    agent = SimpleNamespace(handle_request=AsyncMock(side_effect=handle_request))
    monkeypatch.setattr(server_module, "prepare_command", lambda command: command)
    return reset, provider, agent


def _loop_context(provider):
    settings = deepcopy(get_settings())
    settings.set("config.disable_auto_feedback", False)
    return {"settings": settings, "git_provider": {API_URL: provider}}


async def test_gitlab_loop_stops_when_repo_settings_are_unverified(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, gitlab_webhook)
    reset.side_effect = lambda: setattr(provider, "_gitlab_settings_unverified", True)
    monkeypatch.setattr(gitlab_webhook, "get_pr_commands", lambda _name: COMMANDS)
    monkeypatch.setattr(gitlab_webhook, "should_process_pr_logic", lambda _data: True)

    with request_cycle_context(_loop_context(provider)):
        result = await gitlab_webhook._perform_commands_gitlab("pr_commands", agent, API_URL, {}, {})

    assert result is False
    reset.assert_called_once()
    agent.handle_request.assert_not_awaited()



def test_gitlab_settings_reload_preserves_original_source_and_nested_ref():
    from pr_agent.git_providers.gitlab_provider import GitLabProvider

    provider = GitLabProvider.__new__(GitLabProvider)
    provider._resolved_config_branch = "settings-a"
    provider._repo_settings_failed = False
    cached = [("local", b"[ignore]\nglob=['private/**']")]
    provider._repo_settings_source_options = {
        "use_global_settings_file": True,
        "use_group_settings_file": False,
        "global_settings_repo": "shared",
        "config_branch": "",
    }
    observed = []

    def reload_settings():
        observed.append(get_settings().get("config.use_global_settings_file"))
        provider._resolved_config_branch = "settings-b"
        provider._repo_settings_failed = False
        return cached

    provider.get_repo_settings = Mock(side_effect=reload_settings)
    provider._per_directory_settings_policy_enabled = True
    provider._per_directory_settings_source_branch = "config"
    provider._repo_root_settings_branch = "config"
    provider.id_project = "group/repo"
    provider.gl = MagicMock()
    provider.gl.projects.get.return_value.commits.get.return_value.id = "settings-b"
    provider._nested_settings_equal = Mock(return_value=False)
    settings = deepcopy(get_settings())
    settings.set("config.use_global_settings_file", False)
    settings.set("config.enable_per_directory_settings", True)
    with request_cycle_context({"settings": settings, "repo_settings": cached}):
        provider.reset_diff_cache_for_command()
        assert get_settings().get("config.use_global_settings_file") is False

    assert observed == [True]
    assert provider._gitlab_settings_unverified is True
    assert provider._resolved_config_branch == "settings-a"


def test_gitlab_settings_reload_retries_transient_errors_with_original_sources():
    from pr_agent.git_providers.gitlab_provider import GitLabProvider

    provider = GitLabProvider.__new__(GitLabProvider)
    provider._resolved_config_branch = "settings-a"
    provider._repo_settings_failed = False
    cached = [("local", b"[ignore]\nglob=['private/**']")]
    provider._repo_settings_source_options = {
        "use_global_settings_file": True,
        "use_group_settings_file": False,
        "global_settings_repo": "shared",
        "config_branch": "",
    }
    calls = []

    def reload_settings():
        calls.append(get_settings().get("config.use_global_settings_file"))
        if len(calls) == 1:
            raise OSError("timeout")
        provider._resolved_config_branch = "settings-a"
        provider._repo_settings_failed = False
        return cached

    provider.get_repo_settings = Mock(side_effect=reload_settings)
    settings = deepcopy(get_settings())
    settings.set("config.use_global_settings_file", False)
    settings.set("config.enable_per_directory_settings", False)
    with request_cycle_context({"settings": settings, "repo_settings": cached}):
        provider.reset_diff_cache_for_command()

    assert provider._gitlab_settings_unverified is False
    assert calls == [True, True]
    assert provider._resolved_config_branch == "settings-a"


def test_gitlab_settings_reload_uses_immutable_tree_when_root_was_absent():
    from pr_agent.git_providers.gitlab_provider import GitLabProvider

    provider = GitLabProvider.__new__(GitLabProvider)
    provider._resolved_config_branch = "initial-revision"
    provider._repo_settings_failed = False
    cached = [("global", b"[ignore]\nglob=['private/**']")]
    provider._repo_settings_source_options = {
        "use_global_settings_file": True,
        "use_group_settings_file": False,
        "global_settings_repo": "shared",
        "config_branch": "",
    }

    def reload_settings():
        provider._resolved_config_branch = ""
        provider._repo_settings_failed = False
        return cached

    def get_tree(_ref):
        provider._resolved_config_branch = "moved-revision"
        return ["private/.pr_agent.toml"], "moved-revision"

    provider.get_repo_settings = Mock(side_effect=reload_settings)
    provider._per_directory_settings_policy_enabled = True
    provider._per_directory_settings_source_branch = "policy"
    provider.id_project = "group/repo"
    provider.gl = MagicMock()
    provider.gl.projects.get.return_value.commits.get.return_value.id = "moved-revision"
    provider.gl.projects.get.return_value.files.get.side_effect = GitlabGetError(response_code=404)
    provider._nested_settings_equal = Mock(return_value=False)
    settings = deepcopy(get_settings())
    settings.set("config.enable_per_directory_settings", True)
    with request_cycle_context({"settings": settings, "repo_settings": cached}):
        provider.reset_diff_cache_for_command()

    assert provider._gitlab_settings_unverified is True
    assert provider._resolved_config_branch == "initial-revision"
    provider._nested_settings_equal.assert_called_once_with("initial-revision", "moved-revision")


async def test_gitlab_loop_resets_between_auto_commands(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, gitlab_webhook)
    monkeypatch.setattr(gitlab_webhook, "get_pr_commands", lambda _name: COMMANDS)
    monkeypatch.setattr(gitlab_webhook, "should_process_pr_logic", lambda _data: True)

    with request_cycle_context(_loop_context(provider)):
        await gitlab_webhook._perform_commands_gitlab("pr_commands", agent, API_URL, {}, {})

    assert reset.call_count == len(COMMANDS)
    assert agent.handle_request.await_count == len(COMMANDS)
    assert provider.command_urls == [None] * len(COMMANDS)



@pytest.mark.parametrize("server_module", [gitlab_webhook, gitea_app])
async def test_auto_command_loop_stops_after_unexpected_provider_refresh_failure(monkeypatch, server_module):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, server_module)
    reset.side_effect = ValueError("bad provider response")
    monkeypatch.setattr(server_module, "get_pr_commands", lambda _name: COMMANDS)
    if server_module is gitlab_webhook:
        monkeypatch.setattr(gitlab_webhook, "should_process_pr_logic", lambda _data: True)

    with request_cycle_context(_loop_context(provider)):
        if server_module is gitlab_webhook:
            result = await gitlab_webhook._perform_commands_gitlab("pr_commands", agent, API_URL, {}, {})
        else:
            result = await gitea_app._perform_commands_gitea("pr_commands", agent, {}, API_URL)

    assert result is False
    reset.assert_called_once()
    agent.handle_request.assert_not_awaited()


@pytest.mark.parametrize("notice_fails", [False, True])
async def test_gitea_loop_stops_when_snapshot_cannot_be_verified(monkeypatch, notice_fails):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, gitea_app)
    event_loop_thread = threading.get_ident()
    notice_threads = []

    def publish_notice(_body):
        notice_threads.append(threading.get_ident())
        assert context["repo_settings"] is original_settings
        if notice_fails:
            raise OSError("notice unavailable")

    provider.publish_comment = Mock(side_effect=publish_notice)
    provider._notice_policy_snapshot_required = False
    reset.side_effect = lambda: setattr(provider, "_line_link_snapshot_unverified", True)
    monkeypatch.setattr(gitea_app, "get_pr_commands", lambda _name: COMMANDS)
    original_settings = []
    data = _loop_context(provider)
    data["settings"].set("config.use_repo_settings_file", False)
    data["settings"].set("config.ignore_pr_title", [])
    with request_cycle_context({
        **data,
        "repo_settings": original_settings,
    }):
        result = await gitea_app._perform_commands_gitea("pr_commands", agent, {}, API_URL)
        assert context["repo_settings"] is original_settings

    assert result is False
    reset.assert_called_once()
    agent.handle_request.assert_not_awaited()
    provider.publish_comment.assert_called_once()
    assert "were not run" in provider.publish_comment.call_args.args[0]
    assert notice_threads and notice_threads[0] != event_loop_thread


async def test_gitea_loop_suppresses_notice_when_target_policy_changes(monkeypatch):
    provider = GiteaProvider.__new__(GiteaProvider)
    provider.__dict__.update(
        owner="owner", repo="repo", pr_number=1, enabled_pr=True,
        sha="head-a", base_sha="base-a", base_ref="main",
        pr=SimpleNamespace(base=SimpleNamespace(sha="base-a", ref="main")),
        repo_api=SimpleNamespace(get_pull_request=Mock(return_value=SimpleNamespace(
            title="Retargeted PR",
            user=SimpleNamespace(login="contributor"),
            head=SimpleNamespace(sha="head-b", ref="feature"),
            base=SimpleNamespace(sha="base-b", ref="release"),
        ))),
        logger=Mock(), publish_comment=Mock(),
    )
    agent = SimpleNamespace(handle_request=AsyncMock(return_value=True))
    seen_targets = []
    monkeypatch.setattr(gitea_app, "get_pr_commands", lambda _name: ["/review"])
    monkeypatch.setattr(gitea_app, "enforce_request_policy", lambda _url, provider: (
        seen_targets.append(provider.pr.base.ref) or False
    ))

    context_data = _loop_context(provider)
    context_data["settings"].set("config.ignore_pr_target_branches", ["release"])
    with request_cycle_context(context_data):
        outcome = await gitea_app._perform_commands_gitea("pr_commands", agent, {}, API_URL)

    assert outcome is False
    assert seen_targets == []
    assert provider.base_ref == "main"
    agent.handle_request.assert_not_awaited()
    provider.publish_comment.assert_not_called()


async def test_gitea_notice_policy_check_does_not_block_the_event_loop(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, gitea_app)
    provider.publish_comment = Mock()
    reset.side_effect = lambda: setattr(provider, "_line_link_snapshot_unverified", True)
    monkeypatch.setattr(gitea_app, "get_pr_commands", lambda _name: ["/review"])
    event_loop_thread = threading.get_ident()
    policy_threads = []

    def policy_check(_url, provider):
        policy_threads.append(threading.get_ident())
        return False

    monkeypatch.setattr(gitea_app, "enforce_request_policy", policy_check)
    provider.get_request_policy_metadata = Mock(return_value={"target_branch": "main"})
    provider._notice_policy_snapshot_required = False
    context_data = _loop_context(provider)
    context_data["settings"].set("config.ignore_pr_title", [])
    context_data["settings"].set("config.ignore_pr_target_branches", ["restricted"])
    with request_cycle_context(context_data):
        result = await gitea_app._perform_commands_gitea("pr_commands", agent, {}, API_URL)

    assert result is RequestOutcome.SKIPPED
    assert policy_threads and policy_threads[0] != event_loop_thread
    provider.publish_comment.assert_not_called()
    agent.handle_request.assert_not_awaited()


async def test_gitea_loop_suppresses_notice_when_policy_metadata_is_unverified(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, gitea_app)
    provider.publish_comment = Mock()
    reset.side_effect = lambda: (
        setattr(provider, "_line_link_snapshot_unverified", True),
        setattr(provider, "_gitea_policy_metadata_unverified", True),
    )
    monkeypatch.setattr(gitea_app, "get_pr_commands", lambda _name: ["/review"])
    policy = Mock(side_effect=AssertionError("Stale PR metadata must not be checked"))
    monkeypatch.setattr(gitea_app, "enforce_request_policy", policy)

    context_data = _loop_context(provider)
    context_data["settings"].set("config.ignore_pr_target_branches", ["restricted"])
    with request_cycle_context(context_data):
        outcome = await gitea_app._perform_commands_gitea("pr_commands", agent, {}, API_URL)

    assert outcome is False
    policy.assert_not_called()
    provider.publish_comment.assert_not_called()
    agent.handle_request.assert_not_awaited()


async def test_gitea_notice_is_suppressed_without_verified_target_rules(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, gitea_app)
    provider.publish_comment = Mock()
    reset.side_effect = lambda: (
        setattr(provider, "_line_link_snapshot_unverified", True),
        setattr(provider, "_gitea_policy_metadata_unverified", True),
    )
    monkeypatch.setattr(gitea_app, "get_pr_commands", lambda _name: ["/review"])
    context_data = _loop_context(provider)
    context_data["settings"].set("config.use_repo_settings_file", True)
    with request_cycle_context(context_data):
        outcome = await gitea_app._perform_commands_gitea("pr_commands", agent, {}, API_URL)

    assert outcome is False
    provider.publish_comment.assert_not_called()
    agent.handle_request.assert_not_awaited()


async def test_gitea_notice_allowed_with_verified_host_policy_only(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, gitea_app)
    provider.publish_comment = Mock()
    provider._notice_policy_snapshot_required = False
    reset.side_effect = lambda: (
        setattr(provider, "_line_link_snapshot_unverified", True),
        setattr(provider, "_gitea_policy_metadata_unverified", True),
    )
    monkeypatch.setattr(gitea_app, "get_pr_commands", lambda _name: ["/review"])
    data = _loop_context(provider)
    data["settings"].set("config.use_repo_settings_file", False)
    for rule in gitea_app.RULE_FIELDS.values():
        data["settings"].set(f"config.{rule}", [])
    with request_cycle_context(data):
        outcome = await gitea_app._perform_commands_gitea("pr_commands", agent, {}, API_URL)

    assert outcome is False
    provider.publish_comment.assert_called_once()
    agent.handle_request.assert_not_awaited()


async def test_gitea_notice_rejects_missing_required_policy_labels(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, gitea_app)
    provider.publish_comment = Mock()
    provider._notice_policy_snapshot_required = False
    provider.get_request_policy_metadata = Mock(return_value={"labels": None})
    reset.side_effect = lambda: setattr(provider, "_line_link_snapshot_unverified", True)
    monkeypatch.setattr(gitea_app, "get_pr_commands", lambda _name: ["/review"])
    policy = Mock(side_effect=AssertionError("Missing labels must not be ignored"))
    monkeypatch.setattr(gitea_app, "enforce_request_policy", policy)
    context_data = _loop_context(provider)
    context_data["settings"].set("config.ignore_pr_labels", ["restricted"])
    with request_cycle_context(context_data):
        outcome = await gitea_app._perform_commands_gitea("pr_commands", agent, {}, API_URL)

    assert outcome is False
    assert "labels" in provider.get_request_policy_metadata.call_args.args[0]
    policy.assert_not_called()
    provider.publish_comment.assert_not_called()
    agent.handle_request.assert_not_awaited()


async def test_gitea_loop_offloads_pr_refresh_and_preserves_request_context(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, gitea_app)
    monkeypatch.setattr(gitea_app, "get_pr_commands", lambda _name: ["/review"])
    event_loop_thread = threading.get_ident()
    refresh_threads = []

    def refresh():
        refresh_threads.append(threading.get_ident())
        assert context.get("diff_files") == "old diff"
        context.pop("diff_files", None)

    reset.side_effect = refresh
    with request_cycle_context({**_loop_context(provider), "diff_files": "old diff"}):
        await gitea_app._perform_commands_gitea("pr_commands", agent, {}, API_URL)
        assert context.get("diff_files") is None

    assert refresh_threads and refresh_threads[0] != event_loop_thread
    agent.handle_request.assert_awaited_once()


@pytest.mark.parametrize("publish_enabled,policy_allowed", [
    (False, True),
    (True, False),
])
async def test_gitea_snapshot_notice_respects_output_and_policy(
    monkeypatch, publish_enabled, policy_allowed,
):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, gitea_app)
    provider.publish_comment = Mock()
    provider._notice_policy_snapshot_required = False
    reset.side_effect = lambda: setattr(provider, "_line_link_snapshot_unverified", True)
    monkeypatch.setattr(gitea_app, "get_pr_commands", lambda _name: ["/review"])
    monkeypatch.setattr(
        gitea_app, "enforce_request_policy", lambda _url, provider: policy_allowed,
    )
    settings = deepcopy(get_settings())
    settings.set("config.disable_auto_feedback", False)
    settings.set("config.publish_output", publish_enabled)
    settings.set("config.ignore_pr_title", [])
    settings.set("config.ignore_pr_target_branches", ["restricted"])
    provider.get_request_policy_metadata = Mock(return_value={"target_branch": "main"})
    with request_cycle_context({"settings": settings, "git_provider": {API_URL: provider}}):
        result = await gitea_app._perform_commands_gitea("pr_commands", agent, {}, API_URL)

    assert result is (False if policy_allowed else RequestOutcome.SKIPPED)
    agent.handle_request.assert_not_awaited()
    provider.publish_comment.assert_not_called()


@pytest.mark.parametrize("scenario,dispatch", [
    ("unchanged_local", True),
    ("loading_disabled_same_local", True),
    ("loading_disabled_changed_local", False),
    ("settings_unavailable", False),
    ("target_moves_during_settings_read", False),
    ("target_recheck_unavailable", False),
])
async def test_gitea_target_verification_uses_cached_local_rules(monkeypatch, scenario, dispatch):
    provider = GiteaProvider.__new__(GiteaProvider)
    original = SimpleNamespace(head=SimpleNamespace(sha="head-a"), base=SimpleNamespace(sha="base-a", ref="main"))
    refreshed = SimpleNamespace(head=SimpleNamespace(sha="head-b"), base=SimpleNamespace(sha="base-b", ref="main"))
    getter = Mock(return_value=refreshed)
    if scenario == "target_moves_during_settings_read":
        moved = SimpleNamespace(base=SimpleNamespace(sha="base-c", ref="main"))
        getter.side_effect = [refreshed, moved]
    elif scenario == "target_recheck_unavailable":
        getter.side_effect = [refreshed, ApiException(status=503)]
    local = "[ignore]\nglob = ['protected/**']\n"
    content = Mock(return_value=local)
    provider.__dict__.update(
        enabled_pr=True, owner="owner", repo="repo", pr_number=1, pr=original,
        sha="head-a", base_sha="base-a", base_ref="main", repo_settings=".pr_agent.toml",
        repo_api=SimpleNamespace(get_pull_request=getter, get_file_content=content),
        logger=Mock(), publish_comment=Mock(),
    )
    provider._get_global_repo_settings = Mock(return_value=b"[pr_reviewer]\nextra_instructions='global'\n")
    cached = provider.get_repo_settings()
    # This test simulates a global policy loaded before command overrides.
    provider._gitea_global_settings_source = ("owner", "shared-policy")
    provider._get_global_repo_settings.reset_mock()
    provider._get_global_repo_settings.side_effect = AssertionError("Do not reload global settings")
    content.reset_mock()
    if scenario == "loading_disabled_changed_local":
        content.return_value = local + "regex = ['new-protection']\n"
    elif scenario == "settings_unavailable":
        content.side_effect = ApiException(status=503)
    settings = deepcopy(get_settings())
    settings.set("config.disable_auto_feedback", False)
    settings.set("config.use_repo_settings_file", not scenario.startswith("loading_disabled"))
    settings.set("config.use_global_settings_file", False)
    agent = SimpleNamespace(handle_request=AsyncMock(return_value=True))
    monkeypatch.setattr(gitea_app, "get_pr_commands", lambda _name: ["/review"])
    monkeypatch.setattr(gitea_app, "prepare_command", lambda command: command)
    monkeypatch.setattr(gitea_app, "enforce_request_policy", lambda *args, **kwargs: True)
    with request_cycle_context({
        "settings": settings, "repo_settings": cached, "git_provider": {API_URL: provider},
    }):
        await gitea_app._perform_commands_gitea("pr_commands", agent, {}, API_URL)
        assert context["repo_settings"] is cached
    assert agent.handle_request.await_count == int(dispatch)
    assert provider.base_sha == ("base-b" if dispatch else "base-a")
    assert provider._line_link_snapshot_unverified is not dispatch
    provider._get_global_repo_settings.assert_not_called()
    content.assert_called_once_with(
        owner="owner", repo="repo", commit_sha="base-b", filepath=".pr_agent.toml", propagate_errors=True,
    )


async def test_gitea_loop_resets_between_auto_commands(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, gitea_app)
    monkeypatch.setattr(gitea_app, "get_pr_commands", lambda _name: COMMANDS)

    with request_cycle_context(_loop_context(provider)):
        await gitea_app._perform_commands_gitea("pr_commands", agent, {}, API_URL)

    assert reset.call_count == len(COMMANDS)
    assert agent.handle_request.await_count == len(COMMANDS)
    assert provider.command_urls == [None] * len(COMMANDS)


async def test_azure_loop_resets_between_auto_commands(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, azuredevops_server_webhook)
    monkeypatch.setattr(azuredevops_server_webhook, "get_pr_commands", lambda _name: COMMANDS)
    monkeypatch.setattr(azuredevops_server_webhook, "apply_repo_settings", lambda _url: None)

    with request_cycle_context(_loop_context(provider)):
        await azuredevops_server_webhook._perform_commands_azure("pr_commands", agent, API_URL, {})

    assert reset.call_count == len(COMMANDS)
    assert agent.handle_request.await_count == len(COMMANDS)
    assert provider.command_urls == [None] * len(COMMANDS)


async def test_bitbucket_loop_resets_between_auto_commands(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, bitbucket_app)

    with request_cycle_context(_loop_context(provider)):
        await bitbucket_app._run_commands_bitbucket(COMMANDS, agent, API_URL, {})

    assert reset.call_count == len(COMMANDS)
    assert agent.handle_request.await_count == len(COMMANDS)
    assert provider.command_urls == [None] * len(COMMANDS)


async def test_bitbucket_server_loop_resets_between_auto_commands(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, bitbucket_server_webhook)
    monkeypatch.setattr(bitbucket_server_webhook, "_process_command", lambda command, _url: command)
    monkeypatch.setattr(bitbucket_server_webhook, "PRAgent", lambda: agent)

    with request_cycle_context(_loop_context(provider)):
        await bitbucket_server_webhook._run_commands_sequentially(COMMANDS, API_URL, {})

    assert reset.call_count == len(COMMANDS)
    assert agent.handle_request.await_count == len(COMMANDS)
    assert provider.command_urls == [None] * len(COMMANDS)


def test_gitlab_nested_policy_state_tracks_latest_effective_application():
    from pr_agent.git_providers.utils import _get_per_directory_settings

    provider = SimpleNamespace(
        get_repo_settings_tree=Mock(return_value=([], "original-revision")),
    )
    data = _loop_context(provider)
    with request_cycle_context(data):
        get_settings().set("config.enable_per_directory_settings", True)
        assert _get_per_directory_settings(provider) == []
        assert provider._per_directory_settings_policy_enabled is True
        get_settings().set("config.enable_per_directory_settings", False)
        assert _get_per_directory_settings(provider) == []
        # A subsequent authoritative settings application must clear the
        # earlier enabled policy rather than latching it for this provider.
        assert provider._per_directory_settings_policy_enabled is False


@pytest.mark.parametrize("read_failed,verify_failed", [(True, False), (False, True)])
def test_incomplete_gitlab_settings_block_manual_commands_before_dispatch(
    monkeypatch, read_failed, verify_failed,
):
    from pr_agent.git_providers import utils as provider_utils

    provider = SimpleNamespace(
        _repo_settings_failed=read_failed,
        _gitlab_settings_unverified=verify_failed,
    )
    monkeypatch.setattr(provider_utils, "get_git_provider_with_context", lambda _url: provider)
    data = _loop_context(provider)
    data["settings"].set("config.use_repo_settings_file", True)
    data["settings"].set("config.enable_per_directory_settings", False)
    data["repo_settings"] = ""
    with request_cycle_context(data):
        with pytest.raises(RuntimeError, match="could not be verified"):
            provider_utils.apply_repo_settings(API_URL)


def test_repo_settings_snapshot_preserves_notice_policy_against_later_cli_changes(monkeypatch):
    from pr_agent.git_providers import utils as provider_utils

    provider = SimpleNamespace(_repo_settings_failed=False)
    monkeypatch.setattr(provider_utils, "get_git_provider_with_context", lambda _url: provider)
    monkeypatch.setattr(provider_utils, "apply_host_settings", lambda: None)
    monkeypatch.setattr(provider_utils, "_restore_per_directory_settings", lambda: None)
    data = _loop_context(provider)
    data["repo_settings"] = ""
    data["settings"].set("config.use_repo_settings_file", True)
    data["settings"].set("config.enable_per_directory_settings", False)
    with request_cycle_context(data):
        provider_utils.apply_repo_settings(API_URL)
        assert provider._notice_policy_snapshot_required is True
        get_settings().set("config.use_repo_settings_file", False)
        for rule in gitea_app.RULE_FIELDS.values():
            get_settings().set(f"config.{rule}", [])
        # The provider keeps the original policy despite command overrides.
        assert provider._notice_policy_snapshot_required is True


async def test_gitea_never_uses_cli_weakened_policy_to_publish_notice(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, gitea_app)
    provider.publish_comment = Mock()
    provider._notice_policy_snapshot_required = True
    reset.side_effect = lambda: setattr(provider, "_line_link_snapshot_unverified", True)
    monkeypatch.setattr(gitea_app, "get_pr_commands", lambda _name: ["/review"])
    data = _loop_context(provider)
    data["settings"].set("config.use_repo_settings_file", False)
    for rule in gitea_app.RULE_FIELDS.values():
        data["settings"].set(f"config.{rule}", [])
    with request_cycle_context(data):
        result = await gitea_app._perform_commands_gitea("pr_commands", agent, {}, API_URL)

    assert result is False
    provider.publish_comment.assert_not_called()
    agent.handle_request.assert_not_awaited()

@pytest.mark.parametrize("server_module", [gitlab_webhook, gitea_app])
@pytest.mark.parametrize("raises", [False, True])
async def test_auto_command_loop_stops_after_command_failure(monkeypatch, server_module, raises):
    from pr_agent.algo.run_details import init_run_details

    init_run_details()
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, server_module)
    agent.handle_request = AsyncMock(side_effect=(
        RuntimeError("command failed") if raises else [False, True]
    ))
    monkeypatch.setattr(server_module, "get_pr_commands", lambda _name: COMMANDS)
    if server_module is gitlab_webhook:
        monkeypatch.setattr(server_module, "should_process_pr_logic", lambda _data: True)

    with request_cycle_context(_loop_context(provider)):
        if server_module is gitlab_webhook:
            outcome = await gitlab_webhook._perform_commands_gitlab("pr_commands", agent, API_URL, {}, {})
        else:
            outcome = await gitea_app._perform_commands_gitea("pr_commands", agent, {}, API_URL)

    assert outcome is False
    assert agent.handle_request.await_count == 1
    assert reset.call_count == 1
