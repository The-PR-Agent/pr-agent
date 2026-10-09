from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from starlette_context import context, request_cycle_context

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.git_providers import github_provider as gp
from pr_agent.git_providers.git_provider import GitProvider, IncrementalPR
from pr_agent.servers import github_app


def _provider(files=(), *, real_files=False):
    provider = gp.GithubProvider.__new__(gp.GithubProvider)
    provider.diff_files = None
    provider.incremental = SimpleNamespace(is_incremental=False)
    provider.pr = SimpleNamespace(
        base=SimpleNamespace(sha="base", ref="main"),
        head=SimpleNamespace(sha="head"),
    )
    provider.repo_obj = Mock()
    provider._get_pr = Mock(return_value=provider.pr)
    provider.repo_obj.compare.return_value = SimpleNamespace(merge_base_commit=provider.pr.base)
    if real_files:
        provider.git_files = None
        provider.pr.get_files = Mock(return_value=list(files))
        provider.pr.changed_files = len(files)
        provider.get_files = Mock(wraps=provider.get_files)
    else:
        provider.get_files = Mock(return_value=list(files))
    provider._get_pr_file_content = Mock(return_value="new\n")
    return provider


def _file(filename="file.py"):
    return SimpleNamespace(
        filename=filename,
        status="added",
        patch="@@ -0,0 +1 @@\n+new\n",
        additions=1,
        deletions=0,
    )


@pytest.mark.parametrize("filenames", [[], ["file.py"]])
@pytest.mark.parametrize("in_request", [False, True])
def test_completed_diff_is_reused(filenames, in_request):
    provider = _provider([_file(name) for name in filenames])

    with request_cycle_context({}) if in_request else nullcontext():
        first = provider._get_diff_files()
        second = provider._get_diff_files()

    assert [file.filename for file in first] == filenames
    assert second is first
    provider.get_files.assert_called_once_with()
    provider.repo_obj.compare.assert_called_once_with("base", "head")


def test_empty_request_cache_is_not_reused_by_another_provider(monkeypatch):
    monkeypatch.setattr(gp, "filter_ignored", lambda files: files)
    monkeypatch.setattr(gp, "is_valid_file", lambda filename: True)

    with request_cycle_context({}):
        first = _provider()
        assert first._get_diff_files() == []

        second = _provider([_file()])
        result = second._get_diff_files()

    assert [file.filename for file in result] == ["file.py"]
    second.get_files.assert_called_once_with()
    second.repo_obj.compare.assert_called_once_with("base", "head")


def _select_incremental_file(provider, file):
    provider.unreviewed_files_map = {file.filename: file}
    provider.incremental.last_seen_commit = SimpleNamespace(sha="reviewed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("commands", "expected"),
    [
        (["/review --ignore.glob='tests/*'", "/improve --ignore.glob=[]"],
         [["src/app.py"], ["src/app.py", "tests/test_app.py"]]),
        (["/review --ignore.glob=[]", "/improve --ignore.glob='tests/*'"],
         [["src/app.py", "tests/test_app.py"], ["src/app.py"]]),
        (["/review --ignore.glob='*'", "/improve --ignore.glob=[]"],
         [[], ["src/app.py", "tests/test_app.py"]]),
        (["/review -i", "/improve"],
         [["tests/test_app.py"], ["src/app.py", "tests/test_app.py"]]),
    ],
)
async def test_auto_commands_rebuild_diff_without_check_runs(monkeypatch, commands, expected):
    settings = deepcopy(get_settings())
    settings.set("github_app.feedback_on_draft_pr", True)
    settings.set("config.disable_auto_feedback", False)
    settings.set("github.publish_as_check_run", False)
    settings.set("github.ratelimit_retries", 1)
    settings.set("ignore.regex", [])
    settings.set("ignore.glob", [])
    settings.set("config.ignore_language_framework", [])
    files = [_file("src/app.py"), _file("tests/test_app.py")]
    provider = _provider(files, real_files=True)
    monkeypatch.setattr(provider, "_get_incremental_commits", lambda: _select_incremental_file(provider, files[1]))
    results = []
    caches_before_command = []

    class Agent:
        async def handle_request(self, api_url, command, notify=None):
            if notify:
                notify()
            assert get_git_provider_with_context(api_url) is provider
            caches_before_command.append((provider.diff_files, context.get("diff_files")))
            if "-i" in command:
                provider.get_incremental_commits(IncrementalPR(True))
            diff = provider.get_diff_files()
            assert provider.get_diff_files() is diff
            assert context["diff_files"] is diff
            results.append(diff)
            return True

    monkeypatch.setattr(github_app, "get_pr_commands", lambda _name: commands)
    monkeypatch.setattr(github_app, "should_process_pr_logic", lambda _body: True)
    monkeypatch.setattr(github_app, "_start_auto_command_check_run", lambda *_args: None)
    monkeypatch.setattr(github_app, "_finish_auto_command_check_run", lambda *_args: None)
    monkeypatch.setattr(github_app, "init_run_details", lambda: None)
    monkeypatch.setattr(github_app, "command_failed", lambda: False)
    api_url = "https://api.github.com/repos/org/repo/pulls/1"
    with request_cycle_context({"settings": settings, "git_provider": {api_url: provider}}):
        assert github_app._check_run_provider(api_url) is None
        result = await github_app._perform_auto_commands_github(
            "pr_commands", Agent(), {"action": "opened", "pull_request": {"draft": False}}, api_url, {}
        )

    assert result is True
    assert [[file.filename for file in diff] for diff in results] == expected
    assert caches_before_command == [(None, None), (None, None)]
    assert results[0] is not results[1]
    assert provider.get_files.call_count == 2
    assert provider.repo_obj.compare.call_count == 2
    provider.pr.get_files.assert_called_once_with()


@pytest.mark.parametrize(
    "provider",
    [Mock(spec=[]), SimpleNamespace(get_incremental_commits=lambda incremental: None)],
)
async def test_auto_commands_skip_legacy_providers_without_scope_reset(monkeypatch, provider):
    settings = deepcopy(get_settings())
    settings.set("github_app.feedback_on_draft_pr", True)
    settings.set("config.disable_auto_feedback", False)
    settings.set("github.publish_as_check_run", True)
    monkeypatch.setattr(github_app, "get_git_provider_with_context", lambda **kwargs: provider)
    monkeypatch.setattr(github_app, "get_pr_commands", lambda _name: ["/review"])
    monkeypatch.setattr(github_app, "should_process_pr_logic", lambda _body: True)
    agent = SimpleNamespace(handle_request=AsyncMock(return_value=True))

    with request_cycle_context({"settings": settings}):
        result = await github_app._perform_auto_commands_github(
            "pr_commands", agent, {}, "https://api.github.com/repos/org/repo/pulls/1", {}
        )

    assert result is True
    agent.handle_request.assert_awaited_once()


def test_github_command_reset_does_not_depend_on_incremental_method_signature():
    provider = _provider()
    provider.diff_files = ["cached"]
    provider.incremental = IncrementalPR(True)
    provider.get_incremental_commits = Mock(side_effect=AssertionError("must not be called"))

    with request_cycle_context({"diff_files": provider.diff_files}):
        provider.reset_diff_cache_for_command()

        assert provider.diff_files is None
        assert "diff_files" not in context
        assert provider.incremental.is_incremental is False
        provider.get_incremental_commits.assert_not_called()


async def test_auto_commands_use_provider_scope_reset_hook(monkeypatch):
    settings = deepcopy(get_settings())
    settings.set("github_app.feedback_on_draft_pr", True)
    settings.set("config.disable_auto_feedback", False)
    settings.set("github.publish_as_check_run", False)
    reset_diff_cache = Mock()
    provider = SimpleNamespace(reset_diff_cache_for_command=reset_diff_cache)
    monkeypatch.setattr(github_app, "get_git_provider_with_context", lambda **kwargs: provider)
    monkeypatch.setattr(github_app, "get_pr_commands", lambda _name: ["/review", "/improve"])
    monkeypatch.setattr(github_app, "should_process_pr_logic", lambda _body: True)
    monkeypatch.setattr(github_app, "_start_auto_command_check_run", lambda *_args: None)
    monkeypatch.setattr(github_app, "_finish_auto_command_check_run", lambda *_args: None)
    monkeypatch.setattr(github_app, "init_run_details", lambda: None)
    monkeypatch.setattr(github_app, "command_failed", lambda: False)
    agent = SimpleNamespace(handle_request=AsyncMock(return_value=True))

    with request_cycle_context({"settings": settings}):
        result = await github_app._perform_auto_commands_github(
            "pr_commands", agent, {}, "https://api.github.com/repos/org/repo/pulls/1", {}
        )

    assert result is True
    assert reset_diff_cache.call_count == 2
    assert agent.handle_request.await_count == 2


@pytest.mark.parametrize(
    ("cached", "expected"),
    [
        ([], None),
        (["cached"], ["cached"]),
    ],
)
def test_default_command_reset_preserves_empty_only_behavior(cached, expected):
    provider = SimpleNamespace(diff_files=list(cached))

    GitProvider.reset_diff_cache_for_command(provider)

    assert provider.diff_files == expected


@pytest.mark.parametrize("cached", [[], ["cached"]])
def test_incremental_scope_reset_invalidates_both_diff_caches(cached, monkeypatch):
    provider = _provider()
    provider.diff_files = list(cached)
    monkeypatch.setattr(provider, "_get_incremental_commits", lambda: None)

    with request_cycle_context({"diff_files": provider.diff_files}):
        provider.get_incremental_commits(IncrementalPR(True))

        assert provider.diff_files is None
        assert "diff_files" not in context


@pytest.mark.parametrize("in_request", [False, True])
def test_nonempty_diff_is_rebuilt_for_full_and_incremental_scopes(monkeypatch, in_request):
    files = [_file("first.py"), _file("second.py")]
    provider = _provider(files, real_files=True)
    provider._get_pr_file_content = Mock(
        side_effect=lambda _file, ref, **_kwargs: "new\n" if ref == "head" else "old\n"
    )
    monkeypatch.setattr(provider, "_get_incremental_commits", lambda: _select_incremental_file(provider, files[1]))

    with request_cycle_context({}) if in_request else nullcontext():
        full = provider.get_diff_files()
        assert [file.filename for file in full] == ["first.py", "second.py"]

        provider.get_incremental_commits(IncrementalPR(True))
        incremental = provider.get_diff_files()
        assert [file.filename for file in incremental] == ["second.py"]
        assert incremental[0].base_file == "old\n"
        assert provider.get_diff_files() is incremental
        if in_request:
            assert context["diff_files"] is incremental

        provider.get_incremental_commits()
        restored = provider.get_diff_files()
        assert [file.filename for file in restored] == ["first.py", "second.py"]
        assert provider.get_diff_files() is restored
        if in_request:
            assert context["diff_files"] is restored

    assert restored is not full
    assert provider.get_files.call_count == 3
    assert provider.repo_obj.compare.call_count == 3
    provider.pr.get_files.assert_called_once_with()


@pytest.mark.parametrize("error_type", [RuntimeError, gp.IncompletePullRequestFilesError])
def test_partial_failure_is_not_cached(monkeypatch, error_type):
    monkeypatch.setattr(gp, "filter_ignored", lambda files: files)
    monkeypatch.setattr(gp, "is_valid_file", lambda filename: True)
    provider = _provider([_file("first.py"), _file("second.py")])
    error = error_type("collection failed")
    provider._get_pr_file_content.side_effect = ["new\n", error]
    with request_cycle_context({}):
        with pytest.raises(error_type):
            provider._get_diff_files()

        assert provider.diff_files is None
        assert "diff_files" not in context

        provider._get_pr_file_content.side_effect = None
        result = provider._get_diff_files()

    assert [file.filename for file in result] == ["first.py", "second.py"]
    assert provider.get_files.call_count == 2
    assert provider.repo_obj.compare.call_count == 2
