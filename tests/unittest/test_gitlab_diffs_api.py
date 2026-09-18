"""Exercise MR diff retrieval through python-gitlab's HTTP and pagination code."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlparse

import gitlab
import pytest
import requests
from requests.adapters import BaseAdapter

from pr_agent.git_providers.git_provider import IncrementalPR
from pr_agent.git_providers.gitlab_provider import GitLabProvider, IncompleteGitLabDiffError


def _change(path, **kwargs):
    return {
        "old_path": path, "new_path": path, "diff": "@@ -1 +1 @@\n-old\n+new\n",
        "new_file": False, "deleted_file": False, "renamed_file": False,
        **kwargs,
    }


class DiffTransport(BaseAdapter):
    def __init__(self, responses):
        super().__init__()
        self.responses = iter(responses)
        self.requests = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        assert request.method == "GET"
        status, payload, headers = next(self.responses)
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode()
        response.headers.update({"Content-Type": "application/json", **headers})
        response.request = request
        return response

    def close(self):
        pass


@pytest.fixture
def provider_factory():
    sessions = []

    def make(responses, count="2", project_id="group/sub/repo"):
        transport = DiffTransport(responses)
        session = requests.Session()
        session.mount("https://", transport)
        sessions.append(session)
        provider = GitLabProvider.__new__(GitLabProvider)
        provider.gl = gitlab.Gitlab("https://gitlab.example/gitlab", private_token="offline-token", session=session)
        provider.id_project = project_id
        provider.id_mr = 7
        project = provider.gl.projects.get(project_id, lazy=True)
        provider.mr = project.mergerequests.get(7, lazy=True)
        if count is not None:
            provider.mr.changes_count = count
        provider.mr.diff_refs = {"base_sha": "base", "start_sha": "start", "head_sha": "head"}
        provider.git_files = None
        provider.diff_files = None
        provider.incremental = IncrementalPR(False)
        provider._expand_submodule_changes = lambda changes: changes
        provider.get_pr_file_content = Mock(side_effect=lambda path, ref: "old\n" if ref == "base" else "new\n")
        return provider, transport

    yield make
    for session in sessions:
        session.close()


def _pages(first, second):
    next_url = "https://gitlab.example/gitlab/api/v4/projects/group%2Fsub%2Frepo/merge_requests/7/diffs?page=2"
    return [(200, first, {"Link": f'<{next_url}>; rel="next"'}), (200, second, {})]


def test_collects_all_pages_using_the_configured_client(provider_factory):
    renamed = _change("new.py", old_path="old.py", renamed_file=True)
    provider, transport = provider_factory(_pages([_change("first.py")], [renamed]))

    files = provider.get_diff_files()

    assert [file.filename for file in files] == ["first.py", "new.py"]
    assert files[1].old_filename == "old.py"
    assert [urlparse(request.url).path for request in transport.requests] == [
        "/gitlab/api/v4/projects/group%2Fsub%2Frepo/merge_requests/7/diffs",
        "/gitlab/api/v4/projects/group%2Fsub%2Frepo/merge_requests/7/diffs",
    ]
    assert urlparse(transport.requests[1].url).query == "page=2"
    assert all(request.headers["PRIVATE-TOKEN"] == "offline-token" for request in transport.requests)
    assert provider.get_diff_files() is files
    assert len(transport.requests) == 2


@pytest.mark.parametrize("project_id", [123, "123"])
def test_numeric_project_identifier(provider_factory, project_id):
    provider, transport = provider_factory([(200, [_change("a.py")], {})], "1", project_id)
    assert provider.get_files() == ["a.py"]
    assert urlparse(transport.requests[0].url).path.endswith("/projects/123/merge_requests/7/diffs")


@pytest.mark.parametrize("method", ["get_files", "get_diff_files", "get_pr_file_paths", "get_relevant_diff"])
@pytest.mark.parametrize("flag", ["too_large", "collapsed"])
def test_omitted_patch_on_later_page_is_rejected_before_use(provider_factory, method, flag):
    provider, transport = provider_factory(_pages([_change("visible.py")], [_change("hidden.py", diff="", **{flag: True})]))

    args = ["hidden.py", "new"] if method == "get_relevant_diff" else []
    with pytest.raises(IncompleteGitLabDiffError, match="omitted diff content"):
        getattr(provider, method)(*args)

    assert provider.git_files is None
    assert provider.diff_files is None
    provider.get_pr_file_content.assert_not_called()
    assert len(transport.requests) == 2


@pytest.mark.parametrize("count", ["1000+", "1+", "3", "0", ""])
def test_incomplete_or_unready_collection_is_not_cached(provider_factory, count):
    provider, _ = provider_factory([(200, [_change("a.py"), _change("b.py")], {})], count)
    with pytest.raises(IncompleteGitLabDiffError):
        provider.get_files()
    assert provider.git_files is None


def test_absent_count_is_distinct_from_an_unready_empty_count(provider_factory):
    provider, _ = provider_factory([(200, [_change("a.py")], {})], count=None)
    assert provider.get_files() == ["a.py"]


def test_complete_empty_merge_request(provider_factory):
    provider, _ = provider_factory([(200, [], {})], count="0")
    assert provider.get_diff_files() == []


def test_unflagged_empty_patch_on_older_servers_retains_reconstruction(provider_factory):
    provider, _ = provider_factory([(200, [_change("a.py", diff="")], {})], count="1")
    files = provider.get_diff_files()
    assert "+new" in files[0].patch
    assert "-old" in files[0].patch


def test_explicit_collapse_is_not_overridden_by_patch_text(provider_factory):
    provider, _ = provider_factory([(200, [_change("a.py", collapsed=True)], {})], count="1")
    with pytest.raises(IncompleteGitLabDiffError):
        provider.get_diff_files()


def test_later_page_failure_does_not_cache_a_prefix_and_can_retry(provider_factory):
    pages = _pages([_change("a.py")], [_change("b.py")])
    provider, transport = provider_factory([pages[0], (503, {"message": "unavailable"}, {}), *pages])
    with pytest.raises(gitlab.GitlabHttpError):
        provider.get_files()
    assert provider.git_files is None
    assert provider.get_files() == ["a.py", "b.py"]
    assert len(transport.requests) == 4


@pytest.mark.parametrize("method", ["get_files", "get_diff_files", "get_pr_file_paths", "get_relevant_diff"])
def test_missing_diffs_endpoint_propagates_without_fallback(provider_factory, method):
    provider, transport = provider_factory([
        (404, {"message": "original missing endpoint"}, {}),
    ])
    args = ["a.py", "new"] if method == "get_relevant_diff" else []
    with pytest.raises(gitlab.GitlabHttpError, match="original missing endpoint") as error:
        getattr(provider, method)(*args)
    assert error.value.response_code == 404
    assert provider.git_files is None
    assert provider.diff_files is None
    provider.get_pr_file_content.assert_not_called()
    assert [urlparse(request.url).path.rsplit("/", 1)[-1] for request in transport.requests] == ["diffs"]


@pytest.mark.parametrize("status", [401, 403, 500])
def test_other_errors_do_not_probe_version_or_use_legacy(provider_factory, status):
    provider, transport = provider_factory([(status, {"message": "denied"}, {})])
    with pytest.raises((gitlab.GitlabHttpError, gitlab.GitlabAuthenticationError)):
        provider.get_files()
    assert len(transport.requests) == 1


def _prepare_incremental(provider):
    provider.mr_commits = [object()]
    provider._incremental_kind = "review"
    provider._find_anchor_note = Mock(return_value=object())
    provider.get_commit_range = Mock(return_value=[object()])
    provider.incremental = IncrementalPR(True)
    provider.incremental.last_seen_commit = SimpleNamespace(sha="previous")
    provider.unreviewed_files_map = {}
    project = Mock()
    project.repository_compare.return_value = {"diffs": [_change("a.py"), _change("b.py"), _change("target-only.py")]}
    provider.gl.projects.get = Mock(return_value=project)


def test_incremental_membership_uses_all_pages(provider_factory):
    provider, _ = provider_factory(_pages([_change("a.py")], [_change("b.py")]))
    _prepare_incremental(provider)
    provider._get_incremental_commits()
    assert set(provider.unreviewed_files_map) == {"a.py", "b.py"}


def test_incremental_filter_propagates_known_incomplete_response(provider_factory):
    provider, _ = provider_factory([(200, [_change("a.py", too_large=True, diff="")], {})], "1")
    _prepare_incremental(provider)
    with pytest.raises(IncompleteGitLabDiffError):
        provider._get_incremental_commits()
    assert provider.unreviewed_files_map == {}


@pytest.mark.parametrize("status", [404, 503])
def test_incremental_filter_retains_best_effort_on_http_failure(provider_factory, status):
    provider, transport = provider_factory([(status, {"message": "unavailable"}, {})])
    _prepare_incremental(provider)
    provider._get_incremental_commits()
    assert set(provider.unreviewed_files_map) == {"a.py", "b.py", "target-only.py"}
    assert [urlparse(request.url).path.rsplit("/", 1)[-1] for request in transport.requests] == ["diffs"]
