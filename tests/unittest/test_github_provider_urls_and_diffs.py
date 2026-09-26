"""
Tests documenting current GithubProvider behavior around URL parsing
and get_diff_files edit-type mapping.

These tests deliberately avoid any network/GitHub API by instantiating
the provider via ``__new__`` and exercising only pure helpers, or by
wiring fake PR/file/repo objects for get_diff_files.
"""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from github import GithubException, RateLimitExceededException

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.git_providers import github_provider
from pr_agent.git_providers.github_provider import GithubProvider, IncompletePullRequestFilesError


def _bare_provider():
    """Create a GithubProvider without running __init__ (no network/auth)."""
    return GithubProvider.__new__(GithubProvider)


# ---------------------------------------------------------------------------
# _parse_pr_url
# ---------------------------------------------------------------------------
class TestParsePrUrl:
    def test_normal_github_html_url(self):
        p = _bare_provider()
        repo, num = p._parse_pr_url("https://github.com/owner/repo/pull/42")
        assert repo == "owner/repo"
        assert num == 42

    def test_github_api_url(self):
        p = _bare_provider()
        repo, num = p._parse_pr_url(
            "https://api.github.com/repos/owner/repo/pulls/7"
        )
        assert repo == "owner/repo"
        assert num == 7

    def test_ghes_api_v3_url(self):
        """GHES-style ``/api/v3`` URLs should be normalized to the same parse."""
        p = _bare_provider()
        repo, num = p._parse_pr_url(
            "https://ghes.example.com/api/v3/repos/acme/widgets/pulls/123"
        )
        assert repo == "acme/widgets"
        assert num == 123

    def test_ghes_html_url(self):
        """A GHES HTML URL is parsed identically to github.com HTML URLs."""
        p = _bare_provider()
        repo, num = p._parse_pr_url(
            "https://ghes.example.com/acme/widgets/pull/9"
        )
        assert repo == "acme/widgets"
        assert num == 9

    def test_query_string_is_ignored(self):
        p = _bare_provider()
        repo, num = p._parse_pr_url(
            "https://github.com/owner/repo/pull/55?diff=split&w=1"
        )
        assert repo == "owner/repo"
        assert num == 55

    def test_trailing_slash(self):
        p = _bare_provider()
        repo, num = p._parse_pr_url("https://github.com/owner/repo/pull/3/")
        assert repo == "owner/repo"
        assert num == 3

    def test_invalid_url_missing_pull_segment(self):
        p = _bare_provider()
        with pytest.raises(ValueError):
            p._parse_pr_url("https://github.com/owner/repo/issues/1")

    def test_invalid_url_non_integer_pr(self):
        p = _bare_provider()
        with pytest.raises(ValueError):
            p._parse_pr_url("https://github.com/owner/repo/pull/not-a-number")

    def test_invalid_url_too_short(self):
        p = _bare_provider()
        with pytest.raises(ValueError):
            p._parse_pr_url("https://github.com/owner")


# ---------------------------------------------------------------------------
# _parse_issue_url
# ---------------------------------------------------------------------------
class TestParseIssueUrl:
    def test_normal_github_html_url(self):
        p = _bare_provider()
        repo, num = p._parse_issue_url(
            "https://github.com/owner/repo/issues/12"
        )
        assert repo == "owner/repo"
        assert num == 12

    def test_github_api_url(self):
        p = _bare_provider()
        repo, num = p._parse_issue_url(
            "https://api.github.com/repos/owner/repo/issues/4"
        )
        assert repo == "owner/repo"
        assert num == 4

    def test_ghes_api_v3_url(self):
        p = _bare_provider()
        repo, num = p._parse_issue_url(
            "https://ghes.example.com/api/v3/repos/acme/widgets/issues/77"
        )
        assert repo == "acme/widgets"
        assert num == 77

    def test_query_string_is_ignored(self):
        p = _bare_provider()
        repo, num = p._parse_issue_url(
            "https://github.com/owner/repo/issues/8?foo=bar"
        )
        assert repo == "owner/repo"
        assert num == 8

    def test_invalid_url_non_integer(self):
        p = _bare_provider()
        with pytest.raises(ValueError):
            p._parse_issue_url(
                "https://github.com/owner/repo/issues/not-a-number"
            )

    def test_invalid_url_wrong_segment(self):
        p = _bare_provider()
        with pytest.raises(ValueError):
            p._parse_issue_url("https://github.com/owner/repo/pull/1")


# ---------------------------------------------------------------------------
# _get_owner_and_repo_path / get_git_repo_url
# ---------------------------------------------------------------------------
class TestRepoPathAndGitUrl:
    def test_owner_repo_from_pr_url(self):
        p = _bare_provider()
        assert (
            p._get_owner_and_repo_path("https://github.com/owner/repo/pull/1")
            == "owner/repo"
        )

    def test_owner_repo_from_issue_url(self):
        p = _bare_provider()
        assert (
            p._get_owner_and_repo_path(
                "https://github.com/owner/repo/issues/2"
            )
            == "owner/repo"
        )

    def test_owner_repo_from_git_url(self):
        p = _bare_provider()
        assert (
            p._get_owner_and_repo_path("https://github.com/owner/repo.git")
            == "owner/repo"
        )

    def test_unknown_url_returns_empty(self):
        p = _bare_provider()
        # No "issues" or "pull" segment and no .git suffix -> empty string,
        # logged as an error but does not raise.
        assert p._get_owner_and_repo_path("https://github.com/owner/repo") == ""

    def test_get_git_repo_url_uses_html_base(self):
        p = _bare_provider()
        p.base_url_html = "https://github.com"
        assert (
            p.get_git_repo_url("https://github.com/owner/repo/pull/1")
            == "https://github.com/owner/repo.git"
        )

    def test_get_git_repo_url_uses_ghes_html_base(self):
        p = _bare_provider()
        p.base_url_html = "https://ghes.example.com"
        assert (
            p.get_git_repo_url("https://ghes.example.com/owner/repo/pull/1")
            == "https://ghes.example.com/owner/repo.git"
        )

    def test_get_git_repo_url_mismatch_returns_empty(self):
        """If derived owner/repo doesn't appear in the input URL, return ''."""
        p = _bare_provider()
        p.base_url_html = "https://github.com"
        # _get_owner_and_repo_path returns "" for this input, so the guard
        # `repo_path not in issues_or_pr_url` triggers the empty-string return.
        assert p.get_git_repo_url("https://github.com/owner/repo") == ""


# ---------------------------------------------------------------------------
# get_diff_files edit_type mapping
# ---------------------------------------------------------------------------
def _make_file(
    filename: str,
    status: str,
    patch: str = "@@ -0,0 +1 @@\n+new",
    additions: int = 1,
    deletions: int = 0,
    previous_filename: str = None,
):
    return SimpleNamespace(
        filename=filename,
        status=status,
        patch=patch,
        additions=additions,
        deletions=deletions,
        previous_filename=previous_filename,
    )


def _make_provider_for_diff(files):
    p = _bare_provider()
    p.diff_files = None
    p.git_files = None
    p.incremental = SimpleNamespace(is_incremental=False)
    p.unreviewed_files_map = {}
    # pr.base/head shas drive repo.compare which we stub out below.
    p.pr = SimpleNamespace(
        base=SimpleNamespace(sha="base-sha"),
        head=SimpleNamespace(sha="head-sha"),
        get_files=lambda: files,
        changed_files=len(files),
    )
    # repo_obj.compare returns an object with a merge_base_commit.
    p.repo_obj = SimpleNamespace(
        compare=lambda b, h: SimpleNamespace(
            merge_base_commit=SimpleNamespace(sha="base-sha")
        )
    )
    return p


@pytest.fixture
def patched_helpers():
    """Patch module-level helpers used by get_diff_files."""
    mod = "pr_agent.git_providers.github_provider"
    with patch(f"{mod}.filter_ignored", side_effect=lambda fs: fs), patch(
        f"{mod}.is_valid_file", return_value=True
    ), patch(f"{mod}.load_large_diff", return_value="LARGE_DIFF"):
        yield


class TestGetDiffFilesEditTypes:
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("added", EDIT_TYPE.ADDED),
            ("removed", EDIT_TYPE.DELETED),
            ("renamed", EDIT_TYPE.RENAMED),
            ("modified", EDIT_TYPE.MODIFIED),
            ("copied", EDIT_TYPE.UNKNOWN),  # any unrecognized status
        ],
    )
    def test_status_to_edit_type(self, patched_helpers, status, expected):
        f = _make_file(f"{status}.py", status)
        p = _make_provider_for_diff([f])
        # Avoid reaching real GitHub for file content.
        p._get_pr_file_content = lambda file, sha, path=None: "content"

        diffs = p.get_diff_files()

        assert len(diffs) == 1
        assert isinstance(diffs[0], FilePatchInfo)
        assert diffs[0].edit_type == expected
        assert diffs[0].filename == f.filename

    def test_missing_patch_triggers_load_large_diff(self, patched_helpers):
        """When file.patch is falsy, load_large_diff fills it in."""
        f = _make_file("big.py", "modified", patch="")
        p = _make_provider_for_diff([f])
        p._get_pr_file_content = lambda file, sha, path=None: "content"

        diffs = p.get_diff_files()

        assert len(diffs) == 1
        assert diffs[0].patch == "LARGE_DIFF"
        assert diffs[0].edit_type == EDIT_TYPE.MODIFIED

    def test_existing_patch_preserved(self, patched_helpers):
        f = _make_file("ok.py", "modified", patch="@@ -1 +1 @@\n-a\n+b")
        p = _make_provider_for_diff([f])
        p._get_pr_file_content = lambda file, sha, path=None: "content"

        diffs = p.get_diff_files()

        assert diffs[0].patch == "@@ -1 +1 @@\n-a\n+b"

    def test_cached_diff_files_short_circuits(self, patched_helpers):
        p = _make_provider_for_diff([])
        sentinel = [FilePatchInfo("a", "b", "p", "f.py")]
        p.diff_files = sentinel
        # No fake _get_pr_file_content needed because it should not be called.
        assert p.get_diff_files() is sentinel

    def test_additions_deletions_propagated(self, patched_helpers):
        f = _make_file("x.py", "modified", additions=5, deletions=2)
        p = _make_provider_for_diff([f])
        p._get_pr_file_content = lambda file, sha, path=None: "content"

        diffs = p.get_diff_files()

        assert diffs[0].num_plus_lines == 5
        assert diffs[0].num_minus_lines == 2


class TestGetDiffFilesContentReads:
    def test_added_file_skips_impossible_base_read(self, patched_helpers):
        f = _make_file("added.py", "added")
        p = _make_provider_for_diff([f])
        spy = Mock(return_value="content")
        p._get_pr_file_content = spy

        p.get_diff_files()

        assert [call.args[1] for call in spy.call_args_list] == ["head-sha"]

    def test_removed_file_skips_impossible_head_read(self, patched_helpers):
        f = _make_file("removed.py", "removed", patch="@@ -1 +0,0 @@\n-old")
        p = _make_provider_for_diff([f])
        spy = Mock(return_value="content")
        p._get_pr_file_content = spy

        p.get_diff_files()

        assert [call.args[1] for call in spy.call_args_list] == ["base-sha"]

    def test_modified_file_still_reads_both_revisions(self, patched_helpers):
        f = _make_file("modified.py", "modified")
        p = _make_provider_for_diff([f])
        spy = Mock(return_value="content")
        p._get_pr_file_content = spy

        p.get_diff_files()

        assert [call.args[1] for call in spy.call_args_list] == ["head-sha", "base-sha"]

    def test_incremental_added_file_still_reads_both_revisions(self, patched_helpers):
        f = _make_file("added.py", "added")
        p = _make_provider_for_diff([f])
        p.incremental = SimpleNamespace(is_incremental=True, last_seen_commit_sha="prev-sha")
        p.unreviewed_files_map = {"added.py": f}
        spy = Mock(return_value="content")
        p._get_pr_file_content = spy

        p.get_diff_files()

        assert [call.args[1] for call in spy.call_args_list] == ["head-sha", "prev-sha"]

    def test_incremental_empty_scope_uses_pr_level_removed_status(self, patched_helpers):
        f = _make_file("removed.py", "removed", patch="@@ -1 +0,0 @@\n-old")
        p = _make_provider_for_diff([f])
        p.incremental = SimpleNamespace(is_incremental=True, last_seen_commit_sha="prev-sha")
        p.unreviewed_files_map = {}
        spy = Mock(return_value="content")
        p._get_pr_file_content = spy

        p.get_diff_files()

        assert [call.args[1] for call in spy.call_args_list] == ["base-sha"]


class TestGetDiffFilesRename:
    """A pure GitHub rename carries no `.patch` and reports 0 additions/0
    deletions, so `previous_filename` is the only place the old path lives."""

    def test_old_filename_set_from_previous_filename(self, patched_helpers):
        f = _make_file(
            "new_dir/module.py", "renamed", patch=None, additions=0, deletions=0,
            previous_filename="old_dir/module.py",
        )
        p = _make_provider_for_diff([f])
        p._get_pr_file_content = lambda file, sha, path=None: "same content\n"

        diffs = p.get_diff_files()

        assert diffs[0].old_filename == "old_dir/module.py"

    def test_original_content_looked_up_at_old_path(self, patched_helpers):
        """The base-commit content fetch must use the pre-rename path: the
        new path does not exist there, only the old one does."""
        f = _make_file(
            "new_dir/module.py", "renamed", patch=None, additions=0, deletions=0,
            previous_filename="old_dir/module.py",
        )
        p = _make_provider_for_diff([f])
        spy = Mock(return_value="same content\n")
        p._get_pr_file_content = spy

        p.get_diff_files()

        original_content_call = spy.call_args_list[-1]
        assert original_content_call.kwargs.get("path") == "old_dir/module.py"

    def test_non_renamed_file_has_no_old_filename(self, patched_helpers):
        f = _make_file("x.py", "modified")
        p = _make_provider_for_diff([f])
        p._get_pr_file_content = lambda file, sha, path=None: "content"

        diffs = p.get_diff_files()

        assert diffs[0].old_filename is None

    def test_incremental_content_looked_up_at_old_path(self, patched_helpers):
        """Same as test_original_content_looked_up_at_old_path, but for the
        incremental-review branch, which has its own call to
        `_get_pr_file_content` and used to skip `path=` entirely."""
        f = _make_file(
            "new_dir/module.py", "renamed", patch=None, additions=0, deletions=0,
            previous_filename="old_dir/module.py",
        )
        p = _make_provider_for_diff([f])
        p.incremental = SimpleNamespace(is_incremental=True, last_seen_commit_sha="prev-sha")
        # get_files() short-circuits to unreviewed_files_map.values() once incremental
        # is on and the map is non-empty, exactly as get_incremental_commits() leaves it
        # (github_provider.py:183: populated with the real file objects, not patches).
        p.unreviewed_files_map = {"new_dir/module.py": f}
        spy = Mock(return_value="same content\n")
        p._get_pr_file_content = spy

        p.get_diff_files()

        original_content_call = spy.call_args_list[-1]
        assert original_content_call.args[1] == "prev-sha"
        assert original_content_call.kwargs.get("path") == "old_dir/module.py"


# ---------------------------------------------------------------------------
# Complete pull-request file collection
# ---------------------------------------------------------------------------
class _RequestContext(dict):
    def __init__(self, values=None, *, exists=True):
        super().__init__(values or {})
        self._exists = exists

    def exists(self):
        return self._exists

    def __eq__(self, other):
        if not isinstance(other, _RequestContext):
            return NotImplemented
        return super().__eq__(other) and self._exists == other._exists

    def __ne__(self, other):
        equal = self.__eq__(other)
        if equal is NotImplemented:
            return NotImplemented
        return not equal


class _FakePullRequest:
    def __init__(self, files, changed_files):
        self.files = files
        self.changed_files = changed_files
        self.get_files_calls = 0

    def get_files(self):
        self.get_files_calls += 1
        if isinstance(self.files, BaseException):
            raise self.files
        return self.files


class _ChangedFilesErrorPullRequest(_FakePullRequest):
    def __init__(self, files, error):
        super().__init__(files, 0)
        self.error = error
        self.changed_files_calls = 0

    @property
    def changed_files(self):
        self.changed_files_calls += 1
        raise self.error

    @changed_files.setter
    def changed_files(self, value):
        pass


class _SequencedFilesPullRequest(_FakePullRequest):
    def __init__(self, outcomes, changed_files):
        super().__init__(None, changed_files)
        self.outcomes = list(outcomes)

    def get_files(self):
        self.get_files_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _SequencedChangedFilesPullRequest:
    def __init__(self, files, outcomes):
        self.files = files
        self.outcomes = list(outcomes)
        self.get_files_calls = 0
        self.changed_files_calls = 0

    def get_files(self):
        self.get_files_calls += 1
        return self.files

    @property
    def changed_files(self):
        self.changed_files_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _ExplodingIterable:
    def __init__(self, error):
        self.error = error
        self.iteration_calls = 0

    def __iter__(self):
        self.iteration_calls += 1
        raise self.error


class _TransientIterable:
    def __init__(self, error, files):
        self.error = error
        self.files = files
        self.iteration_calls = 0

    def __iter__(self):
        self.iteration_calls += 1
        if self.iteration_calls == 1:
            raise self.error
        return iter(self.files)


def _make_provider_for_file_collection(pr, *, incremental=False, unreviewed_files_map=None):
    provider = _bare_provider()
    provider.pr = pr
    provider.git_files = None
    provider.diff_files = None
    provider.incremental = SimpleNamespace(is_incremental=incremental)
    provider.unreviewed_files_map = unreviewed_files_map or {}
    return provider


def _set_request_context(monkeypatch, values=None):
    request_context = _RequestContext(values)
    monkeypatch.setattr(github_provider, "context", request_context)
    return request_context


def test_request_context_equality_includes_existence_state():
    present = _RequestContext({"git_files": ["first"]})
    same = _RequestContext({"git_files": ["first"]})
    absent = _RequestContext({"git_files": ["first"]}, exists=False)

    assert present == same
    assert present != absent
    assert present.__eq__({"git_files": ["first"]}) is NotImplemented
    assert present.__ne__({"git_files": ["first"]}) is NotImplemented


class TestCompletePullRequestFiles:
    @pytest.mark.parametrize(
        ("files", "changed_files"),
        [(["first"], 2), (["first", "second"], 1)],
        ids=["fewer-files-than-reported", "more-files-than-reported"],
    )
    def test_mismatched_count_fails_closed_without_caching(self, monkeypatch, files, changed_files):
        request_context = _set_request_context(monkeypatch)
        pr = _FakePullRequest(files, changed_files)
        provider = _make_provider_for_file_collection(pr)

        with pytest.raises(IncompletePullRequestFilesError):
            provider.get_files()

        assert provider.git_files is None
        assert "git_files" not in request_context
        assert pr.get_files_calls == 1

    @pytest.mark.parametrize("changed_files", [None, "two", True])
    def test_invalid_changed_files_metadata_fails_closed_without_caching(self, monkeypatch, changed_files):
        request_context = _set_request_context(monkeypatch)
        provider = _make_provider_for_file_collection(_FakePullRequest(["first"], changed_files))

        with pytest.raises(IncompletePullRequestFilesError):
            provider.get_files()

        assert provider.git_files is None
        assert "git_files" not in request_context

    def test_changed_files_access_error_propagates_without_caching(self, monkeypatch):
        request_context = _set_request_context(monkeypatch)
        error = RuntimeError("changed_files failed")
        pr = _ChangedFilesErrorPullRequest(["first"], error)
        provider = _make_provider_for_file_collection(pr)

        with pytest.raises(RuntimeError, match="changed_files failed") as raised:
            provider.get_files()

        assert raised.value is error
        assert pr.get_files_calls == 2
        assert pr.changed_files_calls == 2
        assert provider.git_files is None
        assert "git_files" not in request_context

    def test_rate_limit_request_propagates_immediately_without_caching(self, monkeypatch):
        request_context = _set_request_context(monkeypatch)
        error = RateLimitExceededException(403, {"message": "API rate limit exceeded"}, None)
        pr = _FakePullRequest(error, 1)
        provider = _make_provider_for_file_collection(pr)

        with pytest.raises(RateLimitExceededException) as raised:
            provider.get_files()

        assert raised.value is error
        assert pr.get_files_calls == 1
        assert provider.git_files is None
        assert "git_files" not in request_context

    def test_rate_limit_metadata_error_propagates_immediately_without_caching(self, monkeypatch):
        request_context = _set_request_context(monkeypatch)
        error = RateLimitExceededException(403, {"message": "API rate limit exceeded"}, None)
        pr = _ChangedFilesErrorPullRequest(["first"], error)
        provider = _make_provider_for_file_collection(pr)

        with pytest.raises(RateLimitExceededException) as raised:
            provider.get_files()

        assert raised.value is error
        assert pr.get_files_calls == 1
        assert pr.changed_files_calls == 1
        assert provider.git_files is None
        assert "git_files" not in request_context

    def test_non_rate_limit_403_uses_the_ordinary_retry_policy(self, monkeypatch):
        request_context = _set_request_context(monkeypatch)
        error = GithubException(403, {"message": "Resource not accessible by integration"}, None)
        pr = _FakePullRequest(error, 1)
        provider = _make_provider_for_file_collection(pr)

        with pytest.raises(GithubException) as raised:
            provider.get_files()

        assert raised.value is error
        assert pr.get_files_calls == 2
        assert provider.git_files is None
        assert "git_files" not in request_context

    @pytest.mark.parametrize(
        "failure",
        [RuntimeError("request failed"), _ExplodingIterable(RuntimeError("page failed"))],
    )
    def test_file_collection_errors_propagate_after_retry_without_caching(self, monkeypatch, failure):
        request_context = _set_request_context(monkeypatch)
        pr = _FakePullRequest(failure, 1)
        provider = _make_provider_for_file_collection(pr)

        with pytest.raises(RuntimeError) as raised:
            provider.get_files()

        expected_error = failure.error if isinstance(failure, _ExplodingIterable) else failure
        assert raised.value is expected_error
        assert pr.get_files_calls == 2
        if isinstance(failure, _ExplodingIterable):
            assert failure.iteration_calls == 2
        assert provider.git_files is None
        assert "git_files" not in request_context

    def test_transient_file_request_recovers_and_populates_caches(self, monkeypatch):
        request_context = _set_request_context(monkeypatch)
        files = ["first"]
        pr = _SequencedFilesPullRequest([RuntimeError("request failed"), files], len(files))
        provider = _make_provider_for_file_collection(pr)

        assert provider.get_files() == files
        assert pr.get_files_calls == 2
        assert provider.git_files == files
        assert request_context["git_files"] == files

    def test_transient_materialization_error_recovers_and_populates_caches(self, monkeypatch):
        request_context = _set_request_context(monkeypatch)
        files = ["first"]
        iterable = _TransientIterable(RuntimeError("page failed"), files)
        pr = _FakePullRequest(iterable, len(files))
        provider = _make_provider_for_file_collection(pr)

        assert provider.get_files() == files
        assert pr.get_files_calls == 2
        assert iterable.iteration_calls == 2
        assert provider.git_files == files
        assert request_context["git_files"] == files

    def test_transient_changed_files_error_recovers_and_populates_caches(self, monkeypatch):
        request_context = _set_request_context(monkeypatch)
        files = ["first"]
        pr = _SequencedChangedFilesPullRequest(files, [RuntimeError("metadata failed"), len(files)])
        provider = _make_provider_for_file_collection(pr)

        assert provider.get_files() == files
        assert pr.get_files_calls == 2
        assert pr.changed_files_calls == 2
        assert provider.git_files == files
        assert request_context["git_files"] == files

    def test_transient_failure_then_mismatch_fails_closed_without_caching(self, monkeypatch):
        request_context = _set_request_context(monkeypatch)
        pr = _SequencedFilesPullRequest([RuntimeError("request failed"), ["first"]], 2)
        provider = _make_provider_for_file_collection(pr)

        with pytest.raises(IncompletePullRequestFilesError):
            provider.get_files()

        assert pr.get_files_calls == 2
        assert provider.git_files is None
        assert "git_files" not in request_context

    def test_get_diff_files_recovers_within_collection_retry(self, monkeypatch, patched_helpers):
        _set_request_context(monkeypatch)
        file = _make_file("first.py", "modified")
        provider = _make_provider_for_diff([file])
        provider.pr.get_files = Mock(side_effect=[RuntimeError("request failed"), [file]])
        provider._get_pr_file_content = lambda file, sha, path=None: "content"

        diffs = provider.get_diff_files()

        assert [diff.filename for diff in diffs] == ["first.py"]
        assert provider.pr.get_files.call_count == 2

    def test_get_diff_files_preserves_completeness_error_without_retrying(self, monkeypatch):
        _set_request_context(monkeypatch)
        pr = _FakePullRequest(["first"], 2)
        provider = _make_provider_for_file_collection(pr)
        settings = Mock()
        settings.get.return_value = 5
        monkeypatch.setattr(github_provider, "get_settings", lambda: settings)

        with pytest.raises(IncompletePullRequestFilesError):
            provider.get_diff_files()

        assert pr.get_files_calls == 1

    def test_matching_count_populates_request_and_instance_caches(self, monkeypatch):
        request_context = _set_request_context(monkeypatch)
        files = ["first", "second"]
        pr = _FakePullRequest(files, len(files))
        provider = _make_provider_for_file_collection(pr)

        assert provider.get_files() == files
        assert provider.git_files == files
        assert request_context["git_files"] == files
        assert pr.get_files_calls == 1

    def test_request_context_cache_avoids_another_api_call(self, monkeypatch):
        files = ["first"]
        _set_request_context(monkeypatch, {"git_files": files})
        pr = _FakePullRequest(RuntimeError("should not fetch"), 1)
        provider = _make_provider_for_file_collection(pr)

        assert provider.get_files() == files
        assert pr.get_files_calls == 0

    def test_none_request_context_cache_is_refetched(self, monkeypatch):
        request_context = _set_request_context(monkeypatch, {"git_files": None})
        files = ["first"]
        pr = _FakePullRequest(files, len(files))
        provider = _make_provider_for_file_collection(pr)

        assert provider.get_files() == files
        assert request_context["git_files"] == files
        assert pr.get_files_calls == 1

    def test_instance_cache_avoids_another_api_call(self, monkeypatch):
        _set_request_context(monkeypatch)
        files = ["first"]
        pr = _FakePullRequest(RuntimeError("should not fetch"), 1)
        provider = _make_provider_for_file_collection(pr)
        provider.git_files = files

        assert provider.get_files() == files
        assert pr.get_files_calls == 0

    @pytest.mark.parametrize("cache_owner", ["request", "instance"])
    def test_empty_cache_is_reused_without_fetching(self, monkeypatch, cache_owner):
        request_context = _set_request_context(monkeypatch, {"git_files": []} if cache_owner == "request" else None)
        pr = _FakePullRequest(RuntimeError("should not fetch"), 0)
        provider = _make_provider_for_file_collection(pr)
        if cache_owner == "instance":
            provider.git_files = []

        assert provider.get_files() == []
        assert pr.get_files_calls == 0
        if cache_owner == "request":
            assert request_context["git_files"] == []

    def test_get_pr_file_paths_uses_complete_collector_during_incremental_review(self, monkeypatch):
        request_context = _set_request_context(monkeypatch)
        files = ["full-file"]
        pr = _FakePullRequest(files, len(files))
        provider = _make_provider_for_file_collection(
            pr,
            incremental=True,
            unreviewed_files_map={"incremental-file": "incremental-file"},
        )

        assert provider.get_pr_file_paths() == files
        assert provider.git_files == files
        assert request_context["git_files"] == files
        assert pr.get_files_calls == 1

    def test_incremental_subset_bypasses_full_collection_and_count_access(self, monkeypatch):
        _set_request_context(monkeypatch)
        error = RuntimeError("changed_files should not be read")
        pr = _ChangedFilesErrorPullRequest([], error)
        file = "incremental-file"
        provider = _make_provider_for_file_collection(
            pr,
            incremental=True,
            unreviewed_files_map={file: file},
        )

        files = provider.get_files()

        assert files == [file]
        assert isinstance(files, list)
        assert pr.get_files_calls == 0
        assert pr.changed_files_calls == 0
