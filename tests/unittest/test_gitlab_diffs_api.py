"""Exercise MR diff retrieval through python-gitlab's HTTP and pagination code."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, call
from urllib.parse import urlparse

import gitlab
import pytest
import requests
from requests.adapters import BaseAdapter

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import IncrementalPR
from pr_agent.git_providers.gitlab_provider import GitLabProvider, IncompleteGitLabDiffError


def _change(path, **kwargs):
    return {
        "old_path": path, "new_path": path, "diff": "@@ -1 +1 @@\n-old\n+new\n",
        "new_file": False, "deleted_file": False, "renamed_file": False,
        **kwargs,
    }


class DiffTransport(BaseAdapter):
    def __init__(self, responses, metadata, metadata_responses=None):
        super().__init__()
        self.responses = iter(responses)
        self.metadata = metadata
        self.metadata_responses = iter(metadata_responses) if metadata_responses is not None else None
        self.requests = []

    @property
    def diff_requests(self):
        return [request for request in self.requests if urlparse(request.url).path.endswith("/diffs")]

    def send(self, request, **kwargs):
        self.requests.append(request)
        assert request.method == "GET"
        if urlparse(request.url).path.endswith("/diffs"):
            status, payload, headers = next(self.responses)
        else:
            assert urlparse(request.url).path.endswith("/merge_requests/7")
            status, payload, headers = (
                next(self.metadata_responses) if self.metadata_responses is not None else (200, self.metadata, {})
            )
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

    def make(responses, count="2", project_id="group/sub/repo", metadata_responses=None):
        metadata = _metadata(count=count)
        transport = DiffTransport(responses, metadata, metadata_responses)
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
        provider._expand_submodule_changes = Mock(side_effect=lambda changes, diff_refs: changes)
        provider.get_pr_file_content = Mock(side_effect=lambda path, ref: "old\n" if ref == "base" else "new\n")
        return provider, transport

    yield make
    for session in sessions:
        session.close()


def _metadata(count="2", **refs):
    return {
        **({"changes_count": count} if count is not None else {}),
        "sha": refs.get("head_sha", "head"),
        "diff_refs": {"base_sha": "base", "start_sha": "start", "head_sha": "head", **refs},
    }


def _metadata_reads(*snapshots):
    return [(200, snapshot, {}) for snapshot in snapshots]


def _pages(first, second):
    next_url = "https://gitlab.example/gitlab/api/v4/projects/group%2Fsub%2Frepo/merge_requests/7/diffs?page=2"
    return [(200, first, {"Link": f'<{next_url}>; rel="next"'}), (200, second, {})]


def test_collects_all_pages_using_the_configured_client(provider_factory):
    renamed = _change("new.py", old_path="old.py", renamed_file=True)
    provider, transport = provider_factory(_pages([_change("first.py")], [renamed]))

    files = provider.get_diff_files()

    assert [file.filename for file in files] == ["first.py", "new.py"]
    assert files[1].old_filename == "old.py"
    assert [urlparse(request.url).path for request in transport.diff_requests] == [
        "/gitlab/api/v4/projects/group%2Fsub%2Frepo/merge_requests/7/diffs",
        "/gitlab/api/v4/projects/group%2Fsub%2Frepo/merge_requests/7/diffs",
    ]
    assert urlparse(transport.diff_requests[1].url).query == "page=2"
    assert len(transport.requests) == 4
    assert all(urlparse(transport.requests[index].url).path.endswith("/merge_requests/7") for index in (0, 3))
    assert all(request.headers["PRIVATE-TOKEN"] == "offline-token" for request in transport.requests)
    assert provider.get_diff_files() is files
    assert len(transport.requests) == 4


@pytest.mark.parametrize("project_id", [123, "123"])
def test_numeric_project_identifier(provider_factory, project_id):
    provider, transport = provider_factory([(200, [_change("a.py")], {})], "1", project_id)
    assert provider.get_files() == ["a.py"]
    assert urlparse(transport.diff_requests[0].url).path.endswith("/projects/123/merge_requests/7/diffs")


@pytest.mark.parametrize("flag", ["too_large", "collapsed"])
def test_pruned_patch_on_later_page_is_reconstructed_from_both_blobs(provider_factory, flag):
    provider, transport = provider_factory(_pages([_change("visible.py")], [_change("pruned.py", diff="", **{flag: True})]))

    files = provider.get_diff_files()

    assert [file.filename for file in files] == ["visible.py", "pruned.py"]
    assert "-old" in files[1].patch and "+new" in files[1].patch
    provider.get_pr_file_content.assert_has_calls([call("pruned.py", "base"), call("pruned.py", "head")])
    assert len(transport.diff_requests) == 2


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


def test_empty_patch_without_omission_flags_retains_reconstruction(provider_factory):
    provider, _ = provider_factory([(200, [_change("a.py", diff="")], {})], count="1")
    files = provider.get_diff_files()
    assert "+new" in files[0].patch
    assert "-old" in files[0].patch


def test_later_page_failure_does_not_cache_a_prefix_and_can_retry(provider_factory):
    pages = _pages([_change("a.py")], [_change("b.py")])
    provider, transport = provider_factory([pages[0], (503, {"message": "unavailable"}, {}), *pages])
    with pytest.raises(gitlab.GitlabHttpError):
        provider.get_files()
    assert provider.git_files is None
    assert provider.get_files() == ["a.py", "b.py"]
    assert len(transport.diff_requests) == 4


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
    assert [urlparse(request.url).path.rsplit("/", 1)[-1] for request in transport.requests] == ["7", "diffs"]


@pytest.mark.parametrize("status", [401, 403, 500])
def test_other_errors_do_not_probe_version_or_use_legacy(provider_factory, status):
    provider, transport = provider_factory([(status, {"message": "denied"}, {})])
    with pytest.raises((gitlab.GitlabHttpError, gitlab.GitlabAuthenticationError)):
        provider.get_files()
    assert len(transport.requests) == 2


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
    provider, _ = provider_factory([(200, [_change("a.py")], {})], "2")
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
    assert [urlparse(request.url).path.rsplit("/", 1)[-1] for request in transport.requests] == ["7", "diffs"]


def test_fresh_metadata_supplies_count_and_blob_refs_without_mutating_cached_mr(provider_factory):
    fresh = _metadata(count="1", base_sha="fresh-base", head_sha="fresh-head")
    provider, transport = provider_factory(
        [(200, [_change("a.py", diff="")], {})], count="1000+", metadata_responses=_metadata_reads(fresh, fresh),
    )
    cached_mr = provider.mr
    cached_refs = dict(cached_mr.diff_refs)
    provider.get_pr_file_content.side_effect = lambda path, ref: "old\n" if ref == "fresh-base" else "new\n"

    files = provider.get_diff_files()

    assert [file.filename for file in files] == ["a.py"]
    assert "-old" in files[0].patch and "+new" in files[0].patch
    assert provider.mr is cached_mr
    assert provider.mr.changes_count == "1000+"
    assert provider.mr.diff_refs == cached_refs
    assert provider.get_pr_file_content.call_args_list == [call("a.py", "fresh-base"), call("a.py", "fresh-head")]
    assert provider._expand_submodule_changes.call_args.args[1] == fresh["diff_refs"]
    assert len(transport.requests) == 3


@pytest.mark.parametrize("field", ["sha", "head_sha", "base_sha", "start_sha", "changes_count"])
def test_moving_metadata_discards_pages_and_retries_once(provider_factory, field):
    old = _metadata()
    fresh = _metadata()
    if field == "changes_count":
        old[field] = "1"
    elif field == "sha":
        fresh[field] = "pushed-head"
    else:
        fresh["diff_refs"][field] = f"fresh-{field}"
    provider, transport = provider_factory(
        [*_pages([_change("discard-a.py")], [_change("discard-b.py")]),
         *_pages([_change("a.py")], [_change("b.py")])],
        metadata_responses=_metadata_reads(old, fresh, fresh, fresh),
    )

    assert provider.get_files() == ["a.py", "b.py"]
    assert len(transport.diff_requests) == 4
    assert len(transport.requests) == 8


@pytest.mark.parametrize("method", ["get_files", "get_diff_files", "get_pr_file_paths", "get_relevant_diff"])
def test_continuous_movement_fails_before_use_and_can_recover(provider_factory, method):
    old, middle, latest = (_metadata(count="1", head_sha=head) for head in ("old", "middle", "latest"))
    provider, transport = provider_factory(
        [(200, [_change("a.py")], {})] * 3,
        metadata_responses=_metadata_reads(old, middle, middle, latest, latest, latest),
    )
    args = ["a.py", "new"] if method == "get_relevant_diff" else []

    with pytest.raises(IncompleteGitLabDiffError, match="changed while collecting"):
        getattr(provider, method)(*args)

    assert provider.git_files is None and provider.diff_files is None
    provider.get_pr_file_content.assert_not_called()
    provider._expand_submodule_changes.assert_not_called()
    assert len(transport.diff_requests) == 2
    assert provider.get_files() == ["a.py"]


@pytest.mark.parametrize("refs", [None, [], {}, {"base_sha": "base"}, {"base_sha": "", "head_sha": "head"},
                                  {"base_sha": "base", "head_sha": 123}])
@pytest.mark.parametrize("method", ["get_files", "get_diff_files", "get_pr_file_paths", "get_relevant_diff"])
def test_nonempty_collection_requires_usable_fresh_refs(provider_factory, refs, method):
    metadata = {**_metadata(count="1"), "diff_refs": refs}
    provider, _ = provider_factory(
        [(200, [_change("a.py")], {})], metadata_responses=_metadata_reads(metadata, metadata),
    )
    args = ["a.py", "new"] if method == "get_relevant_diff" else []
    with pytest.raises(IncompleteGitLabDiffError, match="diff refs are not ready"):
        getattr(provider, method)(*args)
    assert provider.git_files is None and provider.diff_files is None
    provider.get_pr_file_content.assert_not_called()


def test_empty_collection_does_not_require_blob_refs(provider_factory):
    metadata = {"changes_count": "0", "diff_refs": None}
    provider, _ = provider_factory([(200, [], {})], metadata_responses=_metadata_reads(metadata, metadata))
    assert provider.get_diff_files() == []
    provider.get_pr_file_content.assert_not_called()


@pytest.mark.parametrize("after_pages", [False, True])
def test_metadata_http_failure_never_caches_or_uses_pages(provider_factory, after_pages):
    metadata = _metadata(count="1")
    metadata_responses = _metadata_reads(metadata) if after_pages else []
    metadata_responses.append((503, {"message": "metadata unavailable"}, {}))
    provider, transport = provider_factory([(200, [_change("a.py")], {})], metadata_responses=metadata_responses)

    with pytest.raises(gitlab.GitlabHttpError, match="metadata unavailable"):
        provider.get_diff_files()

    assert provider.git_files is None and provider.diff_files is None
    provider.get_pr_file_content.assert_not_called()
    assert len(transport.diff_requests) == int(after_pages)


@pytest.mark.parametrize("flag", ["too_large", "collapsed"])
@pytest.mark.parametrize("excluded", ["uv.lock", "ignored.py"])
def test_flagged_excluded_file_does_not_abort_or_fetch_blobs(provider_factory, monkeypatch, flag, excluded):
    monkeypatch.setitem(get_settings().ignore, "glob", ["ignored.py"])
    provider, _ = provider_factory(
        _pages([_change("visible.py")], [_change(excluded, diff="", **{flag: True})]),
    )

    assert [file.filename for file in provider.get_diff_files()] == ["visible.py"]
    assert all(entry.args[0] != excluded for entry in provider.get_pr_file_content.call_args_list)


@pytest.mark.parametrize("ref", ["base_sha", "start_sha", "head_sha"])
def test_incremental_ref_movement_falls_back_to_fresh_full_collection(provider_factory, ref):
    fresh = _metadata(**{ref: "fresh-ref"})
    changes = [_change("a.py", diff=""), _change("b.py")]
    provider, transport = provider_factory(
        [(200, changes, {})] * 2, metadata_responses=_metadata_reads(fresh, fresh, fresh, fresh),
    )
    _prepare_incremental(provider)
    provider.git_files = ["stale.py"]
    provider.diff_files = [object()]
    provider.unreviewed_files_map = {"stale.py": _change("stale.py")}

    provider._get_incremental_commits()

    assert provider.incremental.is_incremental is False
    assert provider.git_files is None and provider.diff_files is None
    assert provider.unreviewed_files_map == {}
    assert [file.filename for file in provider.get_diff_files()] == ["a.py", "b.py"]
    provider.get_pr_file_content.assert_has_calls([
        call("a.py", fresh["diff_refs"]["base_sha"]), call("a.py", fresh["diff_refs"]["head_sha"]),
    ])
    assert len(transport.diff_requests) == 2
    assert len(transport.requests) == 6
