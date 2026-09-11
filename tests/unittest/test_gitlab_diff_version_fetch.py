from types import SimpleNamespace
from unittest.mock import Mock

from pr_agent.git_providers.gitlab_provider import GitLabProvider


def test_set_merge_request_fetches_only_latest_diff_version():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider._parse_merge_request_url = Mock(return_value=("group/repo", 42))

    latest_diff = object()
    list_diffs = Mock(return_value=[latest_diff])
    mr = SimpleNamespace(diffs=SimpleNamespace(list=list_diffs))
    provider._get_merge_request = Mock(return_value=mr)

    provider._set_merge_request("https://gitlab.example/group/repo/-/merge_requests/42")

    assert provider.id_project == "group/repo"
    assert provider.id_mr == 42
    assert provider.mr is mr
    assert provider.last_diff is latest_diff
    list_diffs.assert_called_once_with(page=1, per_page=1, get_all=False)
