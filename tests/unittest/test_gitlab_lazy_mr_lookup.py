from unittest.mock import Mock

from pr_agent.git_providers.gitlab_provider import GitLabProvider


def test_merge_request_lookup_uses_lazy_project_reference():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.gl = Mock()
    provider.id_project = "group/sub/repo"
    provider.id_mr = 7
    expected = provider.gl.projects.get.return_value.mergerequests.get.return_value

    assert provider._get_merge_request() is expected
    provider.gl.projects.get.assert_called_once_with("group/sub/repo", lazy=True)
    provider.gl.projects.get.return_value.mergerequests.get.assert_called_once_with(7)
