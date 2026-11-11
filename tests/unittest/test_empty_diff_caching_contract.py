"""Empty diffs cache within a command and refresh across command boundaries (#4860).

GitHub and GitLab always cached a completed empty diff; Bitbucket guarded its cache
with a truthiness check and GitLab's provisional path returned uncached, so repeated
get_diff_files() calls in one command re-fetched — and could even see different file
sets. The contract now matches #4373's boundary: consistent inside a command, fresh
across commands via reset_diff_cache_for_command().
"""

from unittest.mock import MagicMock

from pr_agent.git_providers.bitbucket_provider import BitbucketProvider
from pr_agent.git_providers.bitbucket_server_provider import BitbucketServerProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider


def test_bitbucket_server_empty_diff_is_served_from_the_cache():
    provider = BitbucketServerProvider.__new__(BitbucketServerProvider)
    provider.diff_files = []
    provider.pr = MagicMock()

    assert provider.get_diff_files() == []
    assert provider.get_diff_files() == []
    # The cached empty verdict makes no API calls; the old truthiness guard fell
    # through to pr.fromRef on every call.
    provider.pr.assert_not_called()


def test_bitbucket_empty_diff_caches_after_the_first_call():
    calls = {"diffstat": 0, "diff": 0}
    pr = MagicMock()
    pr.diffstat.side_effect = lambda: (calls.__setitem__("diffstat", calls["diffstat"] + 1), [])[-1]
    pr.diff.side_effect = lambda *a, **k: (calls.__setitem__("diff", calls["diff"] + 1), "")[-1]
    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.diff_files = None
    provider._pending_diffstat = None
    provider.pr = pr

    results = [provider.get_diff_files() for _ in range(3)]

    assert results == [[], [], []]
    assert calls == {"diffstat": 1, "diff": 1}


def test_bitbucket_non_empty_diff_still_caches():
    calls = {"diffstat": 0}
    pr = MagicMock()
    pr.diffstat.side_effect = lambda: (
        calls.__setitem__("diffstat", calls["diffstat"] + 1),
        [],
    )[-1]
    pr.diff.return_value = ""
    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.diff_files = None
    provider._pending_diffstat = None
    provider.pr = pr
    provider.diff_files = [MagicMock()]

    provider.get_diff_files()
    provider.get_diff_files()

    assert calls["diffstat"] == 0


def _gitlab_provider(changes):
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.diff_files = None
    provider.mr = MagicMock()
    provider.mr.diff_refs = {"base_sha": "b", "head_sha": "h"}
    provider._get_merge_request_changes = MagicMock(side_effect=changes)
    provider._expand_submodule_changes = MagicMock(side_effect=lambda c, r: c)
    file_obj = MagicMock()
    file_obj.decode.return_value = b"line\n"
    provider.gl = MagicMock()
    provider.gl.projects.get.return_value.files.get.return_value = file_obj
    return provider


def _empty_changes():
    return {"changes": [], "diff_refs": {"base_sha": "b", "head_sha": "h"}, "changes_count": None}


def _ready_changes():
    return {
        "changes": [{
            "old_path": "a.py", "new_path": "a.py",
            "diff": "@@ -1 +1 @@\n-old\n+new\n",
            "new_file": False, "deleted_file": False, "renamed_file": False,
        }],
        "diff_refs": {"base_sha": "b", "head_sha": "h"},
        "changes_count": "1",
    }


def test_gitlab_provisional_empty_diff_is_consistent_within_a_command():
    provider = _gitlab_provider([_empty_changes()])

    first = provider.get_diff_files()
    second = provider.get_diff_files()

    assert first == [] and second == []
    assert provider._get_merge_request_changes.call_count == 1


def test_gitlab_command_boundary_re_polls_a_provisional_empty_diff():
    provider = _gitlab_provider([_empty_changes(), _ready_changes()])

    assert provider.get_diff_files() == []

    provider.reset_diff_cache_for_command()
    after_boundary = provider.get_diff_files()

    assert [f.filename for f in after_boundary] == ["a.py"]
    assert provider._get_merge_request_changes.call_count == 2
