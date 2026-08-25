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

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.git_providers.github_provider import GithubProvider


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
