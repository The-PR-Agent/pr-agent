from types import SimpleNamespace
from unittest.mock import MagicMock

from pr_agent.git_providers.gitlab_provider import GitLabProvider


def test_get_pr_file_content_uses_lazy_project_handle():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "group/repo"

    file_obj = MagicMock()
    file_obj.decode.return_value = b"hello\n"
    project = MagicMock()
    project.files.get.return_value = file_obj
    projects = MagicMock()
    projects.get.return_value = project
    provider.gl = SimpleNamespace(projects=projects)

    assert provider.get_pr_file_content("src/app.py", "deadbeef") == "hello\n"

    projects.get.assert_called_once_with("group/repo", lazy=True)
    project.files.get.assert_called_once_with("src/app.py", "deadbeef")
