"""
Tests documenting current GithubProvider behavior around URL parsing
and get_diff_files edit-type mapping.

These tests deliberately avoid any network/GitHub API by instantiating
the provider via ``__new__`` and exercising only pure helpers, or by
wiring fake PR/file/repo objects for get_diff_files.
"""
import hashlib
import json
import re
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qsl, urlsplit

import pytest
from github import GithubException, RateLimitExceededException
from github.PullRequest import PullRequest
from github.Repository import Repository
from requests.exceptions import ConnectionError as RequestsConnectionError

from pr_agent.algo.pr_processing import pr_generate_extended_diff
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import github_provider
from pr_agent.git_providers.github_provider import GithubProvider, IncompletePullRequestFilesError
from pr_agent.log import get_logger


def _bare_provider():
    """Create a GithubProvider without running __init__ (no network/auth)."""
    return GithubProvider.__new__(GithubProvider)


@pytest.mark.parametrize(("url", "repo_name", "is_issue"), [
    ("https://github.com/acme/pull-tools/issues/42", "acme/pull-tools", True),
    ("https://github.com/pull/repo/issues/42", "pull/repo", True),
    ("https://api.github.com/repos/acme/pull-tools/issues/42", "acme/pull-tools", True),
    ("https://ghes.example.com/api/v3/repos/acme/pull-tools/issues/42", "acme/pull-tools", True),
    ("https://ghes.example.com/acme/pull-tools/issues/42", "acme/pull-tools", True),
    ("https://github.com/acme/repo/issues/42?note=pull#pull", "acme/repo", True),
    ("https://github.com/acme/issues-helper/pull/42", "acme/issues-helper", False),
    ("https://api.github.com/repos/acme/issues-helper/pulls/42", "acme/issues-helper", False),
    ("https://ghes.example.com/api/v3/repos/acme/issues-helper/pulls/42", "acme/issues-helper", False),
    ("https://api.octo.ghe.com/repos/acme/pull-tools/issues/42", "acme/pull-tools", True),
    ("https://api.octo.ghe.com/repos/acme/issues-helper/pulls/42", "acme/issues-helper", False),
    ("https://api.example.com/acme/issues-helper/pull/42", "acme/issues-helper", False),
])
def test_constructor_routes_by_url_resource(monkeypatch, url, repo_name, is_issue):
    client = Mock()
    repo = client.get_repo.return_value
    repo.get_pull.return_value.get_commits.return_value = [Mock()]
    monkeypatch.setattr(GithubProvider, "_get_github_client", lambda self: client)

    provider = GithubProvider(url)

    client.get_repo.assert_called_once_with(repo_name)
    if is_issue:
        repo.get_issue.assert_called_once_with(42)
        repo.get_pull.assert_not_called()
        assert provider.issue_main is repo.get_issue.return_value
    else:
        repo.get_pull.assert_called_once_with(42)
        repo.get_issue.assert_not_called()
        assert provider.pr is repo.get_pull.return_value


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

    def test_api_v3_in_query_does_not_change_url_classification(self):
        p = _bare_provider()
        assert p._parse_pr_url("https://github.com/owner/repo/pull/123?note=/api/v3") == ("owner/repo", 123)

    def test_api_v30_path_is_not_github_api_prefix(self):
        p = _bare_provider()
        with pytest.raises(ValueError):
            p._parse_pr_url("https://github.com/api/v30/repos/owner/repo/pulls/1")

    def test_api_hostname_lookalike_is_not_github_api(self):
        p = _bare_provider()
        with pytest.raises(ValueError):
            p._parse_pr_url("https://fakeapi.github.com.evil.net/repos/owner/repo/pulls/1")

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

    def test_api_v3_in_query_does_not_change_url_classification(self):
        p = _bare_provider()
        assert p._parse_issue_url("https://github.com/owner/repo/issues/123?note=/api/v3") == ("owner/repo", 123)

    def test_api_v30_path_is_not_github_api_prefix(self):
        p = _bare_provider()
        with pytest.raises(ValueError):
            p._parse_issue_url("https://github.com/api/v30/repos/owner/repo/issues/1")

    def test_api_hostname_lookalike_is_not_github_api(self):
        p = _bare_provider()
        with pytest.raises(ValueError):
            p._parse_issue_url("https://fakeapi.github.com.evil.net/repos/owner/repo/issues/1")

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
    @pytest.mark.parametrize(("url", "repo_name"), [
        ("https://github.com/acme/issues-helper/pull/42", "acme/issues-helper"),
        ("https://api.github.com/repos/acme/issues-helper/pulls/42", "acme/issues-helper"),
        ("https://ghes.example.com/api/v3/repos/acme/issues-helper/pulls/42", "acme/issues-helper"),
        ("https://github.com/acme/repo/pull/42?note=issues#issues", "acme/repo"),
        ("https://github.com/acme/pull-tools.git", "acme/pull-tools"),
        ("https://github.com/acme/issues-helper.git", "acme/issues-helper"),
    ])
    def test_repo_path_ignores_resource_words_outside_resource_segment(self, url, repo_name):
        assert _bare_provider()._get_owner_and_repo_path(url) == repo_name

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

    @pytest.mark.parametrize(("base_url", "pr_url", "git_url"), [
        ("https://api.github.com", "https://github.com/owner/repo/pull/1", "https://github.com/owner/repo.git"),
        ("https://ghes.example.com/api/v3", "https://ghes.example.com/owner/repo/pull/1",
         "https://ghes.example.com/owner/repo.git"),
        ("https://api.octo.ghe.com", "https://octo.ghe.com/owner/repo/pull/1", "https://octo.ghe.com/owner/repo.git"),
    ])
    def test_get_git_repo_url_follows_configured_base_url(self, monkeypatch, base_url, pr_url, git_url):
        monkeypatch.setattr(GithubProvider, "_get_github_client", lambda self: Mock())
        settings = get_settings()
        previous = settings.get("GITHUB.BASE_URL")
        settings.set("GITHUB.BASE_URL", base_url)
        try:
            provider = GithubProvider()
        finally:
            settings.set("GITHUB.BASE_URL", previous)
        assert provider.get_git_repo_url(pr_url) == git_url

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
        base=SimpleNamespace(sha="base-sha", ref="main"),
        head=SimpleNamespace(sha="head-sha"),
        get_files=lambda: files,
        changed_files=len(files),
    )
    p._get_pr = lambda: p.pr
    # repo_obj.compare returns an object with a merge_base_commit.
    p.repo_obj = SimpleNamespace(
        compare=lambda b, h: SimpleNamespace(
            merge_base_commit=SimpleNamespace(sha="base-sha")
        )
    )
    # The batched content read needs a repository name and a GraphQL client; this one finds nothing.
    p.repo = "owner/repo"
    p.github_client = SimpleNamespace(_Github__requester=_GraphqlRequester())
    return p


class _GraphqlRequester:
    """Answer GraphQL file-entry queries from a map of "sha:path" to blob, like PyGithub's requester does.

    `requestJson` returns the HTTP status, the headers and the body text. A blob can carry a "mode" for its
    tree entry; without one it is a regular file. A blob can instead be {"entry": ...}, a tree entry served
    as is (a submodule, a directory), or {"commit": None}, a commit that cannot be resolved, whose entry is
    null without an error. A path that is not in the map is answered the way the real API does: its file is
    null and a NOT_FOUND error comes back in a 200 response, next to the entries that were found. Only the
    fields the document selects are returned, so a field missing from the query is missing from the answer.

    Once `fail_after` requests were answered, a call raises `error`, or answers `response` with `status`:
    a string is sent as is, anything else as JSON.
    """

    graphql_url = "https://api.github.com/graphql"

    def __init__(self, blobs=None, error=None, fail_after=0, response=None, status=200):
        self.blobs = blobs or {}
        self.error = error
        self.fail_after = fail_after
        self.response = response
        self.status = status
        self.queries = []

    def requestJson(self, method, url, input):  # noqa: N802
        assert (method, url) == ("POST", self.graphql_url)
        query, variables = input["query"], input["variables"]
        self.queries.append((query, variables))
        failing = len(self.queries) > self.fail_after
        if self.error and failing:
            raise self.error
        if self.response is not None and failing:
            body = self.response if isinstance(self.response, str) else json.dumps(self.response)
            return self.status, {}, body
        aliases = re.findall(r"(\w+): object\(oid: \$(\w+)\) \{ \.\.\. on Commit \{ file\(path: \$(\w+)\)", query)
        selection = re.search(
            r"file\(path: \$\w+\)\s*\{\s*(.*?)\s*object\s*\{\s*\.\.\.\s*on Blob\s*\{\s*(.*?)\s*\}", query
        )
        assert selection, query
        entry_fields, blob_fields = (set(fields.split()) for fields in selection.groups())
        repository, errors = {}, []
        for alias, oid, path in aliases:
            commit = self._commit(variables[oid], variables[path], entry_fields, blob_fields)
            repository[alias] = commit
            if isinstance(commit, dict) and commit.get("file") is None:
                errors.append({"type": "NOT_FOUND", "path": ["repository", alias], "message": "Could not resolve"})
        payload = {"data": {"repository": repository}}
        if errors:
            payload["errors"] = errors
        return 200, {}, json.dumps(payload)

    def _commit(self, oid, path, entry_fields, blob_fields):
        blob = self.blobs.get(f"{oid}:{path}")
        if blob is None:
            return {"file": None}
        if "commit" in blob:
            return blob["commit"]
        if "entry" in blob:
            return {"file": blob["entry"]}
        entry = {"object": {k: v for k, v in blob.items() if k in blob_fields}}
        if "mode" in entry_fields:
            entry["mode"] = blob.get("mode", 0o100644)
        return {"file": entry}

    @property
    def reads(self):
        return [(variables[key], variables[key.replace("oid", "path")])
                for _, variables in self.queries for key in variables if key.startswith("oid")]


def _blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _text_blob(text):
    return {"text": text, "oid": _blob_id(text.encode()), "isBinary": False, "isTruncated": False}


@contextmanager
def _captured_logs():
    """Collect the log messages written inside the block."""
    captured = []
    sink_id = get_logger().add(lambda message: captured.append(str(message)), format="{message}")
    try:
        yield captured
    finally:
        get_logger().remove(sink_id)


def _make_provider_with_graphql(files, requester, rest=None):
    """A diff provider that reads contents through a fake GraphQL requester, then a fake REST client."""
    p = _make_provider_for_diff(files)
    p.github_client = SimpleNamespace(_Github__requester=requester)
    p.repo_obj.full_name = "owner/repo"
    p.repo_obj.get_contents = Mock(
        side_effect=lambda path, ref: SimpleNamespace(decoded_content=(rest or {}).get((ref, path), b"rest"))
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
        p._get_pr_file_content = lambda file, sha, path=None, **kwargs: "content"

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

    def test_filtered_lockfile_name_is_available_without_ignored_paths(self):
        files = [
            _make_file("package.json", "modified"),
            _make_file("pnpm-lock.yaml", "modified"),
            _make_file("ignored.lock", "modified"),
        ]
        p = _make_provider_for_diff(files)
        p._get_pr_file_content = Mock(return_value="content")
        mod = "pr_agent.git_providers.github_provider"
        with patch(f"{mod}.filter_ignored", side_effect=lambda entries: entries[:2]):
            diffs = p.get_diff_files()

        assert [file.filename for file in diffs] == ["package.json"]
        assert p.get_filtered_diff_file_names() == ["pnpm-lock.yaml"]
        assert [call.args[0].filename for call in p._get_pr_file_content.call_args_list] == ["package.json"] * 2

    def test_missing_api_counts_use_lf_boundaries(self, patched_helpers):
        f = _make_file(
            "x.py", "modified",
            patch="@@ -1 +1 @@\n-old\n+new\u2028+continued\u2029-continued\n",
        )
        del f.additions
        del f.deletions
        p = _make_provider_for_diff([f])
        p._get_pr_file_content = lambda file, sha, path=None: "content"

        diff_file = p.get_diff_files()[0]

        assert (diff_file.num_plus_lines, diff_file.num_minus_lines) == (1, 1)

    def test_additions_deletions_propagated(self, patched_helpers):
        f = _make_file("x.py", "modified", additions=5, deletions=2)
        p = _make_provider_for_diff([f])
        p._get_pr_file_content = lambda file, sha, path=None: "content"

        diffs = p.get_diff_files()

        assert diffs[0].num_plus_lines == 5
        assert diffs[0].num_minus_lines == 2


class TestGetDiffFilesContentReads:
    @pytest.mark.parametrize("api_patch", [None, "@@ -0,0 +1 @@\n+new"])
    def test_added_file_skips_impossible_base_read(self, patched_helpers, api_patch):
        f = _make_file("added.py", "added", patch=api_patch)
        p = _make_provider_for_diff([f])
        spy = Mock(return_value="content")
        p._get_pr_file_content = spy

        p.get_diff_files()

        assert [call.args[1] for call in spy.call_args_list] == ["head-sha"]

    @pytest.mark.parametrize("api_patch", [None, "@@ -1 +0,0 @@\n-old"])
    def test_removed_file_skips_impossible_head_read(self, patched_helpers, api_patch):
        f = _make_file("removed.py", "removed", patch=api_patch)
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


class TestGetDiffFilesContentFailures:
    @pytest.fixture
    def content_provider(self):
        file = _make_file("changed.py", "modified", patch=None)
        provider = _make_provider_for_diff([file])
        provider.repo = provider.repo_obj.full_name = "owner/repo"
        provider.repo_obj.get_contents = Mock(return_value=SimpleNamespace(decoded_content=b"value = 1\n"))
        return provider, file

    @pytest.mark.parametrize("incremental,failed_ref,error", [
        (False, "base-sha", GithubException(503, {}, {})),
        (False, "head-sha", GithubException(503, {}, {})),
        (False, "base-sha", GithubException(404, {}, {})),
        (False, "head-sha", GithubException(404, {}, {})),
        (False, "head-sha", UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")),
        (False, "head-sha", AssertionError("unsupported encoding: none")),
        (True, "prev-sha", GithubException(503, {}, {})),
        (True, "head-sha", GithubException(503, {}, {})),
    ])
    def test_reconstruction_keeps_failed_reads_visible(self, content_provider, incremental, failed_ref, error):
        provider, file = content_provider
        if incremental:
            file.patch = "@@ -1 +1 @@\n-old\n+new"
            provider.incremental = SimpleNamespace(is_incremental=True, last_seen_commit_sha="prev-sha")
            provider.unreviewed_files_map = {file.filename: file}

        def get_contents(_path, ref):
            if ref == failed_ref:
                raise error
            return SimpleNamespace(decoded_content=b"value = 1\n")

        provider.repo_obj.get_contents.side_effect = get_contents
        diffs = provider.get_diff_files()

        assert len(diffs) == 1
        assert diffs[0].patch == diffs[0].base_file == diffs[0].head_file == ""
        assert diffs[0].content_fetch_failed is True
        token_handler = SimpleNamespace(prompt_tokens=0, count_tokens=len)
        patches, _, _ = pr_generate_extended_diff([{"files": diffs}], token_handler, False)
        assert "changed.py" in patches[0]
        assert "This file could not be read" in patches[0]
        assert "value = 1" not in patches[0]

    @pytest.mark.parametrize("failed_ref", ["base-sha", "head-sha"])
    def test_existing_patch_survives_optional_content_failure(self, content_provider, failed_ref):
        provider, file = content_provider
        file.patch = "@@ -1 +1 @@\n-old\n+new"

        def get_contents(_path, ref):
            if ref == failed_ref:
                raise GithubException(503, {}, {})
            return SimpleNamespace(decoded_content=b"value = 1\n")

        provider.repo_obj.get_contents.side_effect = get_contents
        diff = provider.get_diff_files()[0]

        assert diff.patch == file.patch
        assert diff.content_fetch_failed is False

    @pytest.mark.parametrize("incremental,empty_ref", [
        (False, "base-sha"), (False, "head-sha"),
        (True, "prev-sha"), (True, "head-sha"),
    ])
    def test_reconstruction_allows_empty_or_absent_snapshot(self, content_provider, incremental, empty_ref):
        provider, file = content_provider
        if incremental:
            provider.incremental = SimpleNamespace(is_incremental=True, last_seen_commit_sha="prev-sha")
            provider.unreviewed_files_map = {file.filename: file}

        def get_contents(_path, ref):
            if ref == empty_ref and incremental:
                raise GithubException(404, {}, {})
            return SimpleNamespace(decoded_content=b"" if ref == empty_ref else b"value = 1\n")

        provider.repo_obj.get_contents.side_effect = get_contents
        diff = provider.get_diff_files()[0]

        assert diff.content_fetch_failed is False
        assert diff.patch
        assert diff.head_file == ("" if empty_ref == "head-sha" else "value = 1\n")
        assert diff.base_file == ("value = 1\n" if empty_ref == "head-sha" else "")


class TestLazyMergeBaseLookup:
    @pytest.mark.parametrize("status", ["added", "removed", "modified"])
    def test_compare_only_when_base_content_is_needed(self, patched_helpers, status):
        file = _make_file(f"{status}.py", status)
        provider = _make_provider_for_diff([file])
        provider.repo_obj.compare = Mock(
            return_value=SimpleNamespace(merge_base_commit=SimpleNamespace(sha="merge-base-sha"))
        )
        read = Mock(return_value="content")
        provider._get_pr_file_content = read

        provider.get_diff_files()

        assert provider.repo_obj.compare.call_count == (0 if status == "added" else 1)
        expected_refs = {
            "added": ["head-sha"],
            "removed": ["merge-base-sha"],
            "modified": ["head-sha", "merge-base-sha"],
        }
        assert [call.args[1] for call in read.call_args_list] == expected_refs[status]

    @pytest.mark.parametrize("files", [
        [],
        [_make_file("image.png", "modified")],
    ])
    def test_empty_or_unsupported_files_skip_compare(self, patched_helpers, files):
        provider = _make_provider_for_diff(files)
        provider.repo_obj.compare = Mock(side_effect=AssertionError("unnecessary compare"))
        provider._get_pr_file_content = Mock(return_value="content")
        if files:
            with patch("pr_agent.git_providers.github_provider.is_valid_file", return_value=False):
                provider.get_diff_files()
        else:
            provider.get_diff_files()
        provider.repo_obj.compare.assert_not_called()

    def test_filtered_files_skip_compare(self, patched_helpers):
        provider = _make_provider_for_diff([_make_file("ignored.py", "modified")])
        provider.repo_obj.compare = Mock(side_effect=AssertionError("unnecessary compare"))
        with patch("pr_agent.git_providers.github_provider.filter_ignored", return_value=[]):
            assert provider.get_diff_files() == []
        provider.repo_obj.compare.assert_not_called()

    def test_incremental_review_skips_merge_base(self, patched_helpers):
        file = _make_file("changed.py", "modified")
        provider = _make_provider_for_diff([file])
        provider.incremental = SimpleNamespace(is_incremental=True, last_seen_commit_sha="prev-sha")
        provider.unreviewed_files_map = {"changed.py": file}
        provider.repo_obj.compare = Mock(side_effect=AssertionError("unnecessary compare"))
        provider._get_pr_file_content = Mock(return_value="content")

        provider.get_diff_files()

        provider.repo_obj.compare.assert_not_called()
        assert [call.args[1] for call in provider._get_pr_file_content.call_args_list] == [
            "head-sha", "prev-sha",
        ]

    def test_merge_base_is_reused_across_content_reads(self, patched_helpers):
        files = [_make_file("a.py", "modified"), _make_file("b.py", "renamed", previous_filename="old.py")]
        provider = _make_provider_for_diff(files)
        provider.repo_obj.compare = Mock(
            return_value=SimpleNamespace(merge_base_commit=SimpleNamespace(sha="merge-base-sha"))
        )
        provider._get_pr_file_content = Mock(return_value="content")

        provider.get_diff_files()

        provider.repo_obj.compare.assert_called_once_with("base-sha", "head-sha")
        assert [call.args[1] for call in provider._get_pr_file_content.call_args_list] == [
            "head-sha", "merge-base-sha", "head-sha", "merge-base-sha",
        ]
        assert provider._get_pr_file_content.call_args_list[-1].kwargs["path"] == "old.py"

    def test_failed_merge_base_keeps_existing_base_fallback(self, patched_helpers):
        file = _make_file("changed.py", "modified")
        provider = _make_provider_for_diff([file])
        provider.repo_obj.compare = Mock(
            side_effect=GithubException(500, {"message": "temporary failure"}, None)
        )
        provider._get_pr_file_content = Mock(return_value="content")

        provider.get_diff_files()

        provider.repo_obj.compare.assert_called_once()
        assert [call.args[1] for call in provider._get_pr_file_content.call_args_list] == [
            "head-sha", "base-sha",
        ]


class TestGetDiffFilesRename:
    """A pure GitHub rename carries no `.patch` and reports 0 additions/0
    deletions, so `previous_filename` is the only place the old path lives."""

    def test_old_filename_set_from_previous_filename(self, patched_helpers):
        f = _make_file(
            "new_dir/module.py", "renamed", patch=None, additions=0, deletions=0,
            previous_filename="old_dir/module.py",
        )
        p = _make_provider_for_diff([f])
        p._get_pr_file_content = lambda file, sha, path=None, **kwargs: "same content\n"

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
        self.head = SimpleNamespace(sha="head-sha")
        self.base = SimpleNamespace(sha="base-sha", ref="main")

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
        self.head = SimpleNamespace(sha="head-sha")
        self.base = SimpleNamespace(sha="base-sha", ref="main")

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
    provider._get_pr = Mock(return_value=pr)
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
        assert pr.get_files_calls == 0
        assert pr.changed_files_calls == 1
        provider._get_pr.assert_not_called()
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
        assert pr.get_files_calls == 0
        assert pr.changed_files_calls == 1
        provider._get_pr.assert_not_called()
        assert provider.git_files is None
        assert "git_files" not in request_context

    @pytest.mark.parametrize("status", [403, 404, 422])
    def test_permanent_client_error_skips_file_collection_retry(self, monkeypatch, status):
        request_context = _set_request_context(monkeypatch)
        error = GithubException(status, {"message": "Resource not accessible"}, None)
        pr = _FakePullRequest(error, 1)
        provider = _make_provider_for_file_collection(pr)

        with pytest.raises(GithubException) as raised:
            provider.get_files()

        assert raised.value is error
        assert pr.get_files_calls == 1
        assert provider.git_files is None
        assert "git_files" not in request_context

    @pytest.mark.parametrize(
        "failure",
        [ValueError("malformed file list"), _ExplodingIterable(RuntimeError("page processing failed"))],
    )
    def test_programming_errors_do_not_retry_or_populate_cache(self, monkeypatch, failure):
        request_context = _set_request_context(monkeypatch)
        pr = _FakePullRequest(failure, 1)
        provider = _make_provider_for_file_collection(pr)

        with pytest.raises((ValueError, RuntimeError)):
            provider.get_files()

        assert pr.get_files_calls == 1
        if isinstance(failure, _ExplodingIterable):
            assert failure.iteration_calls == 1
        assert provider.git_files is None
        assert "git_files" not in request_context

    @pytest.mark.parametrize(
        "failure",
        [RequestsConnectionError("request failed"),
         _ExplodingIterable(RequestsConnectionError("page failed"))],
    )
    def test_file_collection_errors_propagate_after_retry_without_caching(self, monkeypatch, failure):
        request_context = _set_request_context(monkeypatch)
        pr = _FakePullRequest(failure, 1)
        provider = _make_provider_for_file_collection(pr)

        with pytest.raises(RequestsConnectionError) as raised:
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
        pr = _SequencedFilesPullRequest([RequestsConnectionError("request failed"), files], len(files))
        provider = _make_provider_for_file_collection(pr)

        assert provider.get_files() == files
        assert pr.get_files_calls == 2
        assert provider.git_files == files
        assert request_context["git_files"] == files

    def test_transient_materialization_error_recovers_and_populates_caches(self, monkeypatch):
        request_context = _set_request_context(monkeypatch)
        files = ["first"]
        iterable = _TransientIterable(RequestsConnectionError("page failed"), files)
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
        pr = _SequencedChangedFilesPullRequest(
            files, [RequestsConnectionError("metadata failed"), len(files), len(files)]
        )
        provider = _make_provider_for_file_collection(pr)

        assert provider.get_files() == files
        assert pr.get_files_calls == 1
        assert pr.changed_files_calls == 3
        provider._get_pr.assert_called_once()
        assert provider.git_files == files
        assert request_context["git_files"] == files

    def test_transient_failure_then_mismatch_fails_closed_without_caching(self, monkeypatch):
        request_context = _set_request_context(monkeypatch)
        pr = _SequencedFilesPullRequest([RequestsConnectionError("request failed"), ["first"]], 2)
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
        provider.pr.get_files = Mock(side_effect=[RequestsConnectionError("request failed"), [file]])
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


class _RevisionRequester:
    base_url = "https://api.github.test"
    per_page = 30
    is_not_lazy = False

    def __init__(self, moved_field=None):
        self.marker = {"head": "head-a", "base": "base-a", "ref": "main", "count": 31}
        self.moved_field = moved_field
        self.page_reads = []
        self.metadata_reads = 0

    def pull_data(self):
        return {
            "url": f"{self.base_url}/repos/example/project/pulls/7",
            "number": 7,
            "head": {"sha": self.marker["head"]},
            "base": {"sha": self.marker["base"], "ref": self.marker["ref"]},
            "changed_files": self.marker["count"],
        }

    def requestJsonAndCheck(self, method, url, parameters=None, headers=None, **kwargs):  # noqa: N802
        assert method == "GET"
        if url.endswith("/pulls/7"):
            self.metadata_reads += 1
            return {}, self.pull_data()
        assert "/pulls/7/files" in url
        page = 2 if "page=2" in url else 1
        self.page_reads.append(page)
        if page == 2:
            if self.moved_field:
                self.marker[self.moved_field] = 32 if self.moved_field == "count" else "revision-private"
            return {}, [{"filename": "page-two.py", "status": "modified"}]
        return {"link": f'<{url}?page=2>; rel="next"'}, [
            {"filename": f"page-one-{index}.py", "status": "modified"} for index in range(30)
        ]


def _sdk_revision_provider(moved_field=None):
    requester = _RevisionRequester(moved_field)
    pr = PullRequest(requester, {}, requester.pull_data(), completed=True)
    provider = _make_provider_for_file_collection(pr)
    provider.repo = "example/project"
    provider.pr_num = 7
    provider.repo_obj = Repository(
        requester, {}, {"url": f"{requester.base_url}/repos/example/project", "full_name": provider.repo},
        completed=True,
    )
    del provider._get_pr  # Exercise Repository.get_pull and the lazy fresh PullRequest read.
    return provider, requester


@pytest.mark.parametrize("moved_field", ["head", "base", "ref", "count"])
def test_sdk_equal_length_pages_reject_revision_drift_without_caching(monkeypatch, moved_field):
    request_context = _set_request_context(monkeypatch)
    provider, requester = _sdk_revision_provider(moved_field)
    original_pr = provider.pr

    with pytest.raises(IncompletePullRequestFilesError) as raised:
        provider.get_files()

    assert "revision-private" not in str(raised.value)
    assert requester.page_reads == [1, 2]
    assert requester.metadata_reads == 1
    assert provider.pr is original_pr
    assert provider.git_files is None
    assert "git_files" not in request_context


def test_sdk_stable_pages_cache_after_one_fresh_metadata_read(monkeypatch):
    request_context = _set_request_context(monkeypatch)
    provider, requester = _sdk_revision_provider()
    original_pr = provider.pr

    files = provider.get_files()

    assert len(files) == 31
    assert files[-1].filename == "page-two.py"
    assert requester.page_reads == [1, 2]
    assert requester.metadata_reads == 1
    assert provider.git_files is files
    assert request_context["git_files"] is files
    assert provider.get_pr_file_paths() is files
    assert provider.pr is original_pr
    assert requester.metadata_reads == 1
    assert requester.page_reads == [1, 2]



def test_sdk_collects_230_pr_files_in_three_large_pages(monkeypatch):
    _set_request_context(monkeypatch)
    requester = Mock(per_page=100, is_not_lazy=False)
    pr_url = "https://api.github.com/repos/example/project/pulls/7"
    files_url = f"{pr_url}/files"
    all_files = [{"filename": f"file-{index}.py", "status": "modified"} for index in range(230)]

    def fetch_page(method, url, parameters=None, headers=None):
        assert method == "GET"
        query = dict(parse_qsl(urlsplit(url).query))
        query.update(parameters or {})
        assert int(query["per_page"]) == 100
        page = int(query.get("page", 1))
        offset = (page - 1) * 100
        links = {"link": f'<{files_url}?per_page=100&page={page + 1}>; rel="next"'} if page < 3 else {}
        return links, all_files[offset:offset + 100]

    requester.requestJsonAndCheck.side_effect = fetch_page
    pr = PullRequest(requester, {}, {
        "url": pr_url,
        "head": {"sha": "head"},
        "base": {"sha": "base", "ref": "main"},
        "changed_files": len(all_files),
    }, completed=True)
    provider = _make_provider_for_file_collection(pr)

    files = provider.get_files()

    assert [file.filename for file in files] == [file["filename"] for file in all_files]
    assert requester.requestJsonAndCheck.call_count == 3
    assert provider.get_pr_file_paths() is files
    assert requester.requestJsonAndCheck.call_count == 3


@pytest.mark.parametrize("field,value", [
    ("head", None), ("head", ""), ("base", None), ("ref", ""),
    ("count", None), ("count", True), ("count", "1"), ("count", -1),
])
@pytest.mark.parametrize("when", ["original", "fresh"])
def test_invalid_revision_metadata_does_not_enter_caches(monkeypatch, field, value, when):
    request_context = _set_request_context(monkeypatch)
    pr = _FakePullRequest(["first"], 1)
    fresh = _FakePullRequest(["first"], 1)
    target = pr if when == "original" else fresh
    if field == "head":
        target.head.sha = value
    elif field == "base":
        target.base.sha = value
    elif field == "ref":
        target.base.ref = value
    else:
        target.changed_files = value
    provider = _make_provider_for_file_collection(pr)
    provider._get_pr.return_value = fresh

    with pytest.raises(IncompletePullRequestFilesError):
        provider.get_files()

    assert pr.get_files_calls == (0 if when == "original" else 1)
    assert provider._get_pr.call_count == (0 if when == "original" else 1)
    assert provider.git_files is None
    assert "git_files" not in request_context


def test_post_read_failure_recovers_within_the_same_two_attempts(monkeypatch):
    request_context = _set_request_context(monkeypatch)
    pr = _FakePullRequest(["first"], 1)
    provider = _make_provider_for_file_collection(pr)
    provider._get_pr.side_effect = [RequestsConnectionError("metadata request failed"), pr]

    assert provider.get_files() == ["first"]
    assert pr.get_files_calls == 2
    assert provider._get_pr.call_count == 2
    assert request_context["git_files"] == ["first"]


def test_page_and_post_read_failure_share_the_two_attempt_budget(monkeypatch):
    request_context = _set_request_context(monkeypatch)
    pr = _SequencedFilesPullRequest([RequestsConnectionError("page failed"), ["first"]], 1)
    provider = _make_provider_for_file_collection(pr)
    provider._get_pr.side_effect = RequestsConnectionError("metadata request failed")

    with pytest.raises(RequestsConnectionError, match="metadata request failed"):
        provider.get_files()

    assert pr.get_files_calls == 2
    provider._get_pr.assert_called_once()
    assert provider.git_files is None
    assert "git_files" not in request_context


@pytest.mark.parametrize("variables,payload", [
    (None, {"query": "q"}),
    ({"owner": "o"}, {"query": "q", "variables": {"owner": "o"}}),
], ids=["without-variables", "with-variables"])
def test_post_graphql_sends_the_variables_only_when_given(variables, payload):
    calls = []
    requester = SimpleNamespace(
        graphql_url="https://api.github.com/graphql",
        requestJson=lambda method, url, input: calls.append((method, url, input)) or (200, {}, "{}"),
    )
    provider = _bare_provider()
    provider.github_client = SimpleNamespace(_Github__requester=requester)

    assert provider._post_graphql("q", variables) == (200, {}, "{}")
    assert calls == [("POST", "https://api.github.com/graphql", payload)]


class TestBatchedContentReads:
    """`get_diff_files` warms the revision-content cache with GraphQL blob reads; REST stays the fallback."""

    @staticmethod
    def _rest_reads(provider):
        return [(call.kwargs["ref"], call.args[0]) for call in provider.repo_obj.get_contents.call_args_list]

    @staticmethod
    def _modified_files(count):
        return [_make_file(f"f{i}.py", "modified") for i in range(count)]

    def test_modified_files_are_read_in_one_graphql_request(self, patched_helpers):
        blobs = {f"{sha}:f{i}.py": _text_blob(f"{sha} {i} h\u00e9llo \u20ac")
                 for i in range(3) for sha in ("head-sha", "base-sha")}
        requester = _GraphqlRequester(blobs)
        provider = _make_provider_with_graphql(self._modified_files(3), requester)

        diffs = provider.get_diff_files()

        assert len(requester.queries) == 1
        query, _ = requester.queries[0]
        assert "$oid0: GitObjectID!" in query and "$path0: String!" in query
        assert self._rest_reads(provider) == []
        assert [(d.head_file, d.base_file) for d in diffs] == [
            (f"head-sha {i} h\u00e9llo \u20ac", f"base-sha {i} h\u00e9llo \u20ac") for i in range(3)
        ]

    @pytest.mark.parametrize("statuses,graphql_requests", [
        (["modified"], 0),
        (["added", "modified"], 1),
    ], ids=["two-reads", "three-reads"])
    def test_batching_starts_at_three_reads(self, patched_helpers, statuses, graphql_requests):
        files = [_make_file(f"f{i}.py", status) for i, status in enumerate(statuses)]
        requester = _GraphqlRequester()
        provider = _make_provider_with_graphql(files, requester)

        provider.get_diff_files()

        assert len(requester.queries) == graphql_requests

    @pytest.mark.parametrize("blob", [
        None,
        {"commit": None},
        {},
        {"entry": {"mode": 0o160000, "object": None}},
        {"entry": {"mode": 0o40000, "object": {}}},
        {**_text_blob("x"), "isBinary": True},
        {**_text_blob("x"), "isTruncated": True},
        {**_text_blob("x"), "oid": _blob_id(b"y")},
        {**_text_blob("a \ufffd byte"), "oid": _blob_id(b"a \xff byte")},
        {"text": "a\ud800b", "oid": _blob_id(b"a?b"), "isBinary": False, "isTruncated": False},
        {**_text_blob("# \u4e00\u4e8c\u4e09\u56db\u4e94\n"),
         "oid": _blob_id(b"\xff\xfe" + "# \u4e00\u4e8c\u4e09\u56db\u4e94\n".encode("utf-16-le"))},
        {**_text_blob("../target/file.py"), "mode": 0o120000},
        {**_text_blob("target\nwith a newline"), "mode": 0o120000},
        {**_text_blob("x"), "mode": None},
        {"text": None, "oid": _blob_id(b"\xff"), "isBinary": False, "isTruncated": False},
    ], ids=["missing", "unknown-commit", "not-a-blob", "submodule", "directory", "binary", "truncated",
            "re-encoded", "replacement-character", "lone-surrogate", "utf-16-of-equal-size", "symlink",
            "symlink-with-newline", "no-mode", "no-text"])
    def test_unusable_blob_falls_back_to_its_rest_read(self, patched_helpers, blob):
        blobs = {
            "head-sha:f0.py": _text_blob("head 0"), "base-sha:f0.py": _text_blob("base 0"),
            "head-sha:f1.py": blob, "base-sha:f1.py": _text_blob("base 1"),
        }
        provider = _make_provider_with_graphql(
            self._modified_files(2), _GraphqlRequester(blobs), rest={("head-sha", "f1.py"): b"rest head 1"}
        )

        diffs = provider.get_diff_files()

        assert self._rest_reads(provider) == [("head-sha", "f1.py")]
        assert [(d.head_file, d.base_file) for d in diffs] == [("head 0", "base 0"), ("rest head 1", "base 1")]

    @pytest.mark.parametrize("mode", [0o100644, 0o100755], ids=["regular", "executable"])
    def test_text_without_a_newline_is_served_when_the_file_is_not_a_symlink(self, patched_helpers, mode):
        blobs = {f"{sha}:f{i}.py": {**_text_blob("1.2.3"), "mode": mode}
                 for i in range(2) for sha in ("head-sha", "base-sha")}
        provider = _make_provider_with_graphql(self._modified_files(2), _GraphqlRequester(blobs))

        diffs = provider.get_diff_files()

        assert self._rest_reads(provider) == []
        assert [(d.head_file, d.base_file) for d in diffs] == [("1.2.3", "1.2.3")] * 2

    def test_empty_file_is_served_without_a_rest_read(self, patched_helpers):
        blobs = {
            "head-sha:f0.py": _text_blob(""), "base-sha:f0.py": _text_blob(""),
            "head-sha:f1.py": _text_blob("head 1"), "base-sha:f1.py": _text_blob(""),
        }
        provider = _make_provider_with_graphql(self._modified_files(2), _GraphqlRequester(blobs))

        diffs = provider.get_diff_files()

        assert self._rest_reads(provider) == []
        assert [(d.head_file, d.base_file) for d in diffs] == [("", ""), ("head 1", "")]

    def test_a_literal_replacement_character_is_served_when_the_blob_id_matches(self, patched_helpers):
        blobs = {f"{sha}:f{i}.py": _text_blob("a \ufffd byte") for i in range(2) for sha in ("head-sha", "base-sha")}
        provider = _make_provider_with_graphql(self._modified_files(2), _GraphqlRequester(blobs))

        diffs = provider.get_diff_files()

        assert self._rest_reads(provider) == []
        assert [(d.head_file, d.base_file) for d in diffs] == [("a \ufffd byte", "a \ufffd byte")] * 2

    @pytest.mark.parametrize("error", [
        GithubException(502, {"message": "Bad gateway"}, None),
        RequestsConnectionError("connection reset"),
        ValueError("invalid JSON"),
    ], ids=["github", "transport", "malformed-json"])
    def test_graphql_failure_falls_back_to_rest_reads(self, patched_helpers, error):
        provider = _make_provider_with_graphql(self._modified_files(2), _GraphqlRequester(error=error))

        diffs = provider.get_diff_files()

        assert len(self._rest_reads(provider)) == 4
        assert [(d.head_file, d.base_file) for d in diffs] == [("rest", "rest")] * 2

    @pytest.mark.parametrize("status,response", [
        (200, "<html>sign in</html>"),
        (200, ""),
        (200, "null"),
        (200, '"unexpected"'),
        (200, ["unexpected"]),
        (200, {"data": {"repository": None}}),
        (200, {"data": None}),
        (200, {"errors": ["boom"]}),
        (204, ""),
        (403, {"message": "API rate limit exceeded"}),
        (502, "<html>Bad gateway</html>"),
        (502, {"data": {"repository": {f"entry{i}": {"file": {"mode": 0o100644, "object": _text_blob("graphql")}}
                                       for i in range(4)}}}),
        (201, {"data": {"repository": {f"entry{i}": {"file": {"mode": 0o100644, "object": _text_blob("graphql")}}
                                       for i in range(4)}}}),
    ], ids=["not-json", "empty", "null", "json-string", "not-an-object", "no-repository", "no-data", "odd-error",
            "no-content", "forbidden", "bad-gateway", "data-with-an-error-status", "data-with-another-status"])
    def test_unexpected_graphql_payload_falls_back_to_rest_reads(self, patched_helpers, status, response):
        requester = _GraphqlRequester(response=response, status=status)
        provider = _make_provider_with_graphql(self._modified_files(2), requester)

        diffs = provider.get_diff_files()

        assert len(self._rest_reads(provider)) == 4
        assert [(d.head_file, d.base_file) for d in diffs] == [("rest", "rest")] * 2

    @pytest.mark.parametrize("failure", [
        {"error": GithubException(502, {"message": "Bad gateway"}, None)},
        {"response": "<html>sign in</html>"},
    ], ids=["error", "bad-payload"])
    def test_a_failed_batch_leaves_the_remaining_reads_to_rest(self, patched_helpers, monkeypatch, failure):
        monkeypatch.setattr(github_provider, "_GRAPHQL_BLOB_BATCH", 3)
        blobs = {f"{sha}:f{i}.py": _text_blob(f"{sha} {i}") for i in range(4) for sha in ("head-sha", "base-sha")}
        requester = _GraphqlRequester(blobs, fail_after=1, **failure)
        provider = _make_provider_with_graphql(self._modified_files(4), requester)

        diffs = provider.get_diff_files()

        assert len(requester.queries) == 2
        assert len(self._rest_reads(provider)) == 5
        assert (diffs[0].head_file, diffs[0].base_file) == ("head-sha 0", "base-sha 0")
        assert (diffs[1].head_file, diffs[1].base_file) == ("head-sha 1", "rest")

    @pytest.mark.parametrize("raised", [True, False], ids=["raised", "returned"])
    def test_a_failed_batch_logs_the_error_without_the_response(self, patched_helpers, raised):
        leaked = "API_KEY=do-not-log"
        partial = {"data": {"repository": {"entry0": {"file": {"object": _text_blob(leaked)}}}},
                   "errors": [{"type": "TIMEOUT", "message": "timed out"}]}
        failure = {"error": GithubException(400, partial, None)} if raised else {"response": partial}
        provider = _make_provider_with_graphql(self._modified_files(2), _GraphqlRequester(**failure))
        expected = ("GithubException", "status=400") if raised else ("status=200", "TIMEOUT")

        with _captured_logs() as captured:
            provider.get_diff_files()

        assert any("Batched content read failed" in line and all(part in line for part in expected)
                   for line in captured)
        assert not any(leaked in line for line in captured)

    def test_a_response_without_a_repository_is_logged_and_left_to_rest(self, patched_helpers):
        requester = _GraphqlRequester(response={"data": {"repository": None}})
        provider = _make_provider_with_graphql(self._modified_files(2), requester)

        with _captured_logs() as captured:
            provider.get_diff_files()

        assert any("returned no repository" in line for line in captured)
        assert len(self._rest_reads(provider)) == 4

    def test_several_missing_paths_do_not_discard_the_batch(self, patched_helpers):
        blobs = {f"{sha}:f{i}.py": _text_blob(f"{sha} {i}") for i in (0, 3) for sha in ("head-sha", "base-sha")}
        provider = _make_provider_with_graphql(self._modified_files(4), _GraphqlRequester(blobs))

        diffs = provider.get_diff_files()

        assert self._rest_reads(provider) == [
            ("head-sha", "f1.py"), ("base-sha", "f1.py"), ("head-sha", "f2.py"), ("base-sha", "f2.py"),
        ]
        assert [(d.head_file, d.base_file) for d in diffs] == [
            ("head-sha 0", "base-sha 0"), ("rest", "rest"), ("rest", "rest"), ("head-sha 3", "base-sha 3"),
        ]

    def test_a_missing_path_next_to_another_error_discards_the_batch(self, patched_helpers):
        partial = {"data": {"repository": {"entry0": {"file": {"mode": 0o100644, "object": _text_blob("partial")}}}},
                   "errors": [{"type": "NOT_FOUND", "message": "gone"}, {"type": "TIMEOUT", "message": "slow"}]}
        requester = _GraphqlRequester(response=partial)
        provider = _make_provider_with_graphql(self._modified_files(3), requester)

        provider.get_diff_files()

        assert len(requester.queries) == 1
        assert len(self._rest_reads(provider)) == 6

    @pytest.mark.parametrize("errors", [
        [{"type": ["NOT_FOUND"]}, "boom", None, {"type": {"k": 1}}, {"type": 5}],
        {"type": "NOT_FOUND"},
        "NOT_FOUND",
        [],
    ], ids=["odd-items", "dict", "string", "empty"])
    def test_odd_graphql_error_shapes_fall_back_to_rest_reads(self, patched_helpers, errors):
        partial = {"data": {"repository": {"entry0": {"file": {"mode": 0o100644, "object": _text_blob("partial")}}}},
                   "errors": errors}
        requester = _GraphqlRequester(response=partial)
        provider = _make_provider_with_graphql(self._modified_files(2), requester)

        provider.get_diff_files()

        assert len(self._rest_reads(provider)) == 4

    def test_unexpected_graphql_error_is_not_swallowed(self, patched_helpers):
        provider = _make_provider_with_graphql(self._modified_files(2), _GraphqlRequester(error=TypeError("bug")))

        with pytest.raises(TypeError):
            provider.get_diff_files()

    def test_requests_are_split_into_bounded_batches(self, patched_helpers, monkeypatch):
        monkeypatch.setattr(github_provider, "_GRAPHQL_BLOB_BATCH", 3)
        requester = _GraphqlRequester()
        provider = _make_provider_with_graphql(self._modified_files(4), requester)

        provider.get_diff_files()

        assert [(len(variables) - 2) // 2 for _, variables in requester.queries] == [3, 3, 2]

    def test_paths_travel_as_variables(self, patched_helpers):
        path = 'dir/we"ird {name} \\ \u00e9.py'
        files = [_make_file(path, "modified"), _make_file("f1.py", "modified")]
        blobs = {f"{sha}:{path}": _text_blob(f"{sha} content") for sha in ("head-sha", "base-sha")}
        requester = _GraphqlRequester(blobs)
        provider = _make_provider_with_graphql(files, requester)

        diffs = provider.get_diff_files()

        query, _ = requester.queries[0]
        assert path not in query
        assert ("head-sha", path) in requester.reads
        assert (diffs[0].head_file, diffs[0].base_file) == ("head-sha content", "base-sha content")

    def test_batch_requests_the_reads_the_loop_makes(self, patched_helpers, monkeypatch):
        monkeypatch.setattr(github_provider, "MAX_FILES_ALLOWED_FULL", 5)
        files = [
            _make_file("added.py", "added"),
            _make_file("removed.py", "removed"),
            _make_file("renamed.py", "renamed", previous_filename="old/renamed.py"),
            _make_file("modified.py", "modified"),
            _make_file("invalid.png", "modified"),
            _make_file("past-limit.py", "modified"),
            _make_file("past-limit-without-patch.py", "modified", patch=""),
        ]

        def collect(requester):
            provider = _make_provider_with_graphql(files, requester)
            with patch.object(github_provider, "is_valid_file", side_effect=lambda name: name != "invalid.png"):
                provider.get_diff_files()
            return provider

        failing = _GraphqlRequester(error=GithubException(502, {"message": "Bad gateway"}, None))
        loop_reads = set(self._rest_reads(collect(failing)))
        requester = _GraphqlRequester({f"{ref}:{path}": _text_blob(path) for ref, path in loop_reads})

        provider = collect(requester)

        assert len(loop_reads) == 8
        assert sorted(requester.reads) == sorted(loop_reads)
        assert self._rest_reads(provider) == []

    def test_incremental_review_does_not_use_graphql(self, patched_helpers):
        files = self._modified_files(3)
        requester = _GraphqlRequester()
        provider = _make_provider_with_graphql(files, requester)
        provider.incremental = SimpleNamespace(is_incremental=True, last_seen_commit_sha="prev-sha")
        provider.unreviewed_files_map = {file.filename: file for file in files}

        provider.get_diff_files()

        assert requester.queries == []
        assert len(self._rest_reads(provider)) == 6

    def test_patch_reconstruction_uses_the_batched_contents(self, patched_helpers):
        files = [_make_file(f"f{i}.py", "modified", patch="") for i in range(2)]
        blobs = {f"{sha}:f{i}.py": _text_blob(f"{sha} {i}") for i in range(2) for sha in ("head-sha", "base-sha")}
        provider = _make_provider_with_graphql(files, _GraphqlRequester(blobs))

        with patch.object(github_provider, "load_large_diff", return_value="rebuilt") as load_large_diff:
            provider.get_diff_files()

        assert self._rest_reads(provider) == []
        assert [call.args for call in load_large_diff.call_args_list] == [
            ("f0.py", "head-sha 0", "base-sha 0"), ("f1.py", "head-sha 1", "base-sha 1"),
        ]

    def test_cached_contents_are_not_requested_again(self, patched_helpers):
        files = self._modified_files(3)
        blobs = {f"{sha}:f{i}.py": _text_blob(f"{sha} {i}") for i in range(3) for sha in ("head-sha", "base-sha")}
        requester = _GraphqlRequester(blobs)
        provider = _make_provider_with_graphql(files, requester)
        provider.get_diff_files()

        provider.diff_files = None
        provider.get_diff_files()

        assert len(requester.queries) == 1
        assert self._rest_reads(provider) == []

    def test_a_recollection_does_not_batch_fewer_than_three_uncached_reads(self, patched_helpers):
        blobs = {f"{sha}:f{i}.py": _text_blob(f"{sha} {i}") for i in range(3) for sha in ("head-sha", "base-sha")}
        blobs["head-sha:f0.py"] = {**_text_blob("x"), "isBinary": True}
        requester = _GraphqlRequester(blobs)
        provider = _make_provider_with_graphql(
            self._modified_files(3), requester, rest={("head-sha", "f0.py"): b""}
        )
        provider.get_diff_files()

        provider.diff_files = None
        provider.get_diff_files()

        assert len(requester.queries) == 1
        assert self._rest_reads(provider) == [("head-sha", "f0.py")] * 2


def test_retry_does_not_adopt_a_marker_changed_after_first_capture(monkeypatch):
    request_context = _set_request_context(monkeypatch)
    pr = _FakePullRequest(["first"], 1)
    provider = _make_provider_for_file_collection(pr)

    def fresh_read():
        if provider._get_pr.call_count == 1:
            pr.head.sha = "moved-head"
            raise RequestsConnectionError("metadata request failed")
        return pr

    provider._get_pr.side_effect = fresh_read
    with pytest.raises(IncompletePullRequestFilesError):
        provider.get_files()

    assert pr.get_files_calls == 2
    assert provider._get_pr.call_count == 2
    assert provider.git_files is None
    assert "git_files" not in request_context


@pytest.mark.parametrize("error", [
    RateLimitExceededException(403, {"message": "rate limited"}, None),
    GithubException(429, {"message": "rate limited"}, None),
])
def test_post_read_rate_limit_propagates_immediately(monkeypatch, error):
    request_context = _set_request_context(monkeypatch)
    pr = _FakePullRequest(["first"], 1)
    provider = _make_provider_for_file_collection(pr)
    provider._get_pr.side_effect = error

    with pytest.raises(type(error)) as raised:
        provider.get_files()

    assert raised.value is error
    assert pr.get_files_calls == 1
    provider._get_pr.assert_called_once()
    assert provider.git_files is None
    assert "git_files" not in request_context
