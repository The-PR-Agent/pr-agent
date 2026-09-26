from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette_context import context, request_cycle_context

from pr_agent.git_providers import github_provider as gp
from pr_agent.git_providers.git_provider import IncrementalPR
from pr_agent.servers import github_app


def _provider(files=()):
    provider = gp.GithubProvider.__new__(gp.GithubProvider)
    provider.diff_files = None
    provider.incremental = SimpleNamespace(is_incremental=False)
    provider.pr = SimpleNamespace(
        base=SimpleNamespace(sha="base"),
        head=SimpleNamespace(sha="head"),
    )
    provider.repo_obj = Mock()
    provider.repo_obj.compare.return_value = SimpleNamespace(merge_base_commit=provider.pr.base)
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


def test_empty_instance_diff_is_reused():
    provider = _provider()

    first = provider._get_diff_files()
    second = provider._get_diff_files()

    assert first == []
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


@pytest.mark.parametrize(
    ("cached_after_first", "expected_before_second"),
    [
        ([], None),
        (["cached"], ["cached"]),
    ],
)
async def test_auto_commands_reset_only_empty_diff_cache_without_check_runs(
    monkeypatch, cached_after_first, expected_before_second
):
    settings = SimpleNamespace(
        github_app=SimpleNamespace(feedback_on_draft_pr=True),
        config=SimpleNamespace(disable_auto_feedback=False),
        set=lambda *args, **kwargs: None,
    )
    command_provider = SimpleNamespace(diff_files=None)
    seen = []

    class Agent:
        async def handle_request(self, _api_url, _command):
            seen.append(command_provider.diff_files)
            command_provider.diff_files = list(cached_after_first)
            return True

    monkeypatch.setattr(github_app, "get_settings", lambda: settings)
    monkeypatch.setattr(github_app, "get_pr_commands", lambda _name: ["/review", "/improve"])
    monkeypatch.setattr(github_app, "should_process_pr_logic", lambda _body: True)
    monkeypatch.setattr(github_app, "prepare_command", lambda command: command)
    monkeypatch.setattr(github_app, "get_git_provider_with_context", lambda **_kwargs: command_provider)
    monkeypatch.setattr(github_app, "_check_run_provider", lambda _api_url: None)
    monkeypatch.setattr(github_app, "_start_auto_command_check_run", lambda *_args: None)
    monkeypatch.setattr(github_app, "_finish_auto_command_check_run", lambda *_args: None)
    monkeypatch.setattr(github_app, "init_run_details", lambda: None)
    monkeypatch.setattr(github_app, "command_failed", lambda: False)

    result = await github_app._perform_auto_commands_github(
        "pr_commands",
        Agent(),
        {"action": "opened", "pull_request": {"draft": False}},
        "https://api.github.com/repos/org/repo/pulls/1",
        {},
    )

    assert result is True
    assert seen == [None, expected_before_second]


@pytest.mark.parametrize(
    ("cached", "expected"),
    [
        ([], None),
        (["cached"], ["cached"]),
    ],
)
def test_incremental_scope_reset_only_invalidates_empty_diff(cached, expected, monkeypatch):
    provider = _provider()
    provider.diff_files = list(cached)
    monkeypatch.setattr(provider, "_get_incremental_commits", lambda: None)

    provider.get_incremental_commits(IncrementalPR(True))

    assert provider.diff_files == expected


@pytest.mark.parametrize("error_type", [RuntimeError, gp.IncompletePullRequestFilesError])
def test_partial_failure_is_not_cached(monkeypatch, error_type):
    monkeypatch.setattr(gp, "filter_ignored", lambda files: files)
    monkeypatch.setattr(gp, "is_valid_file", lambda filename: True)
    provider = _provider([_file("first.py"), _file("second.py")])
    error = error_type("collection failed")
    provider._get_pr_file_content.side_effect = ["new\n", error]
    expected = (
        gp.IncompletePullRequestFilesError
        if error_type is gp.IncompletePullRequestFilesError
        else gp.RateLimitExceeded
    )

    with request_cycle_context({}):
        with pytest.raises(expected):
            provider._get_diff_files()

        assert provider.diff_files is None
        assert "diff_files" not in context

        provider._get_pr_file_content.side_effect = None
        result = provider._get_diff_files()

    assert [file.filename for file in result] == ["first.py", "second.py"]
    assert provider.get_files.call_count == 2
    assert provider.repo_obj.compare.call_count == 2
