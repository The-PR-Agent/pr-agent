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
from starlette_context import context, request_cycle_context

from pr_agent.agent.pr_agent import PRAgent, command2class
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


async def test_gitlab_loop_resets_between_auto_commands(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, gitlab_webhook)
    monkeypatch.setattr(gitlab_webhook, "get_pr_commands", lambda _name: COMMANDS)
    monkeypatch.setattr(gitlab_webhook, "should_process_pr_logic", lambda _data: True)

    with request_cycle_context(_loop_context(provider)):
        await gitlab_webhook._perform_commands_gitlab("pr_commands", agent, API_URL, {}, {})

    assert reset.call_count == len(COMMANDS)
    assert agent.handle_request.await_count == len(COMMANDS)
    assert provider.command_urls == [None] * len(COMMANDS)


async def test_gitea_loop_resets_between_auto_commands(monkeypatch):
    reset, provider, agent = _loop_provider_and_agent(monkeypatch, gitea_app)
    monkeypatch.setattr(gitea_app, "get_pr_commands", lambda _name: COMMANDS)

    with request_cycle_context(_loop_context(provider)):
        await gitea_app._perform_commands_gitea("pr_commands", agent, {}, API_URL)

    assert reset.call_count == len(COMMANDS)
    assert agent.handle_request.await_count == len(COMMANDS)
    assert provider.command_urls == [None] * len(COMMANDS)


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


@pytest.mark.parametrize("change", [
    "head", "target", "retarget", "stable", "failed_refresh", "persistent_refresh", "nonretryable_refresh", "html_url",
    "missing_head", "missing_base", "missing_head_sha", "missing_base_sha", "missing_base_ref",
])
async def test_gitea_commands_use_fresh_diff_and_publication_refs(monkeypatch, change):
    settings = deepcopy(get_settings())
    for key, value in {
        "config.git_provider": "gitea", "config.use_repo_settings_file": True,
        "config.publish_output": False, "config.disable_auto_feedback": False,
        "config.repo_context_files": [], "config.global_settings_repo": "", "config.extra_config_url": "",
        "gitea.url": "https://gitea.com" if change == "html_url" else "https://gitea.example",
        "gitea.web_url": "", "gitea.personal_access_token": "test-token",
        "gitea.repo_setting": ".pr_agent.toml", "skills.enabled": False,
        # Repeated /review commands are consolidated upstream; keep a third command for retry tests.
        "gitea.push_commands": ["/describe", "/review"] + (["/describe"] if "refresh" in change else []),
    }.items():
        settings.set(key, value)
    url = "https://gitea.example/org/repo/pulls/1"
    before = SimpleNamespace(
        head=SimpleNamespace(sha="head-a", ref="refs/pull/1/head"),
        base=SimpleNamespace(sha="target-a", ref="main"), merge_base="merge-a",
        title="original title", body="original description", labels=[],
        html_url=url,
    )
    after = deepcopy(before)
    after.title, after.body = "updated title", "updated description"
    if change in ("head", "failed_refresh", "persistent_refresh", "nonretryable_refresh"):
        after.head.sha = "head-b"
    if change in ("target", "retarget"):
        after.base.sha, after.merge_base = "target-b", "merge-b"
    if change == "retarget":
        after.base.ref = "release"
    if change == "html_url":
        after.html_url = "https://changed.example/org/repo/pulls/1"
    if change == "missing_head":
        after.head = None
    if change == "missing_base":
        after.base = None
    if change == "missing_head_sha":
        after.head.sha = ""
    if change == "missing_base_sha":
        after.base.sha = ""
    if change == "missing_base_ref":
        after.base.ref = ""
    active = before
    pending_refresh_errors = {"failed_refresh": 1, "persistent_refresh": 2, "nonretryable_refresh": 1}.get(change, 0)
    api = MagicMock()

    def read_pr(**_kwargs):
        nonlocal pending_refresh_errors
        if active is after and pending_refresh_errors:
            pending_refresh_errors -= 1
            status = 404 if change == "nonretryable_refresh" else 503
            raise ApiException(status=status, reason="temporarily unavailable")
        return active

    def paths():
        return ["a.py", "new.py"] if active.head.sha == "head-b" else ["a.py"]

    def read_content(*, commit_sha, filepath, **_kwargs):
        if filepath == ".pr_agent.toml":
            return '[pr_description]\nextra_instructions="run policy"\n[pr_reviewer]\nextra_instructions="run policy"'
        return f"{commit_sha}:{filepath}\n"

    def read_diff(**_kwargs):
        return "".join(
            f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n"
            f"-{active.merge_base}:{path}\n+{active.head.sha}:{path}\n" for path in paths()
        )

    api.get_pull_request.side_effect = read_pr
    api.get_change_file_pull_request.side_effect = lambda **_kwargs: [
        {"filename": path, "status": "modified"} for path in paths()
    ]
    api.get_pull_request_diff.side_effect = read_diff
    api.get_file_content.side_effect = read_content
    api.get_languages.return_value = {"Python": 100}
    api.get_pr_commits.side_effect = lambda **_kwargs: [{
        "sha": active.head.sha, "html_url": f"{url}/commits/{active.head.sha}",
        "commit": {"message": f"commit {active.head.sha}"},
    }]
    monkeypatch.setattr("pr_agent.git_providers.gitea_provider.RepoApi", lambda _client: api)
    observed = []
    observed_providers = []

    async def capture(tool):
        nonlocal active
        provider = tool.git_provider
        observed_providers.append(provider)
        files = provider.get_diff_files()
        assert provider.get_diff_files() is files  # Repeated reads within a command still reuse the result.
        assert provider.publish_code_suggestions([{
            "body": "suggestion", "relevant_file": "a.py", "relevant_lines_start": 1,
        }]) is True
        observed.append((
            provider.get_pr_head_sha(), provider.get_files(),
            [(file.filename, file.base_file, file.head_file, file.patch) for file in files],
            api.create_inline_comment.call_args.kwargs["commit_id"], provider.get_line_link("a.py", 1),
            provider.get_latest_commit_url(), tool.vars["title"], provider.get_user_description(),
            provider.get_repo_context_ref(), provider.get_request_policy_metadata({"target_branch"})["target_branch"],
            tool.vars["extra_instructions"], provider.get_pr_url(),
        ))
        active = after

    monkeypatch.setattr(command2class["describe"], "run", capture)
    monkeypatch.setattr(command2class["review"], "run", capture)
    with request_cycle_context({"settings": settings}):
        await gitea_app._perform_commands_gitea("push_commands", PRAgent(ai_handler=SimpleNamespace), {}, url)

    expected = []
    expected_prs = [before, after, after] if change == "failed_refresh" else (
        [before] if change.startswith("missing_") else [before, after])
    for pr in expected_prs:
        names = ["a.py", "new.py"] if pr.head.sha == "head-b" else ["a.py"]
        web_url = "https://changed.example" if pr is after and change == "html_url" else "https://gitea.example"
        expected.append((
            pr.head.sha, names,
            [(path, f"{pr.merge_base}:{path}\n", f"{pr.head.sha}:{path}\n",
              f"@@ -1 +1 @@\n-{pr.merge_base}:{path}\n+{pr.head.sha}:{path}\n") for path in names],
            pr.head.sha, f"{web_url}/org/repo/src/commit/{pr.head.sha}/a.py#L1",
            f"{url}/commits/{pr.head.sha}", pr.title, pr.body, pr.base.sha, pr.base.ref, "run policy",
            f"{web_url}/org/repo/pulls/1",
        ))
    assert observed == expected
    if change.startswith("missing_"):
        provider = observed_providers[0]
        assert provider.pr is before
        assert (provider.sha, provider.base_sha, provider.base_ref) == (
            before.head.sha, before.base.sha, before.base.ref,
        )
    assert api.get_change_file_pull_request.call_count == len(expected_prs)
    assert api.get_pull_request_diff.call_count == len(expected_prs)
    assert api.get_pr_commits.call_count == len(expected_prs)
    if change == "head":
        provider = observed_providers[0]
        provider.filtered_diff_file_names = ["stale.py"]
        provider.reset_diff_cache_for_command()
        assert provider.get_filtered_diff_file_names() == []
    # Raw-diff revision guards may perform additional PR metadata reads. Require
    # the refresh/retry minimum without coupling this test to that separate check.
    assert api.get_pull_request.call_count >= (3 if "refresh" in change else 2) + (
        1 if change in ("failed_refresh", "persistent_refresh") else 0
    )
    # Repository settings keep their existing per-run snapshot, even after a target advance or retarget.
    config_reads = [call.kwargs for call in api.get_file_content.call_args_list
                    if call.kwargs["filepath"] == ".pr_agent.toml"]
    assert len(config_reads) == 1 and config_reads[0]["commit_sha"] == before.base.sha


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
