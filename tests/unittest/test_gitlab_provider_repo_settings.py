from unittest.mock import MagicMock, patch

import pytest
from gitlab.exceptions import GitlabGetError

from pr_agent.git_providers.gitlab_provider import GitLabProvider


def _file(content: bytes):
    project_file = MagicMock()
    project_file.decode.return_value = content
    return project_file


def _provider_with_project(project):
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.gl = MagicMock()
    provider.gl.projects.get.return_value = project
    provider.id_project = "group/repo"
    return provider


@pytest.fixture
def project():
    project = MagicMock()
    project.default_branch = "main"
    return project


@pytest.fixture(autouse=True)
def no_global_settings():
    with patch.object(GitLabProvider, "_get_global_repo_settings", return_value=""):
        yield


def _settings(config_branch):
    settings = MagicMock()
    settings.get.side_effect = lambda key, default=None: (
        config_branch if key == "CONFIG.CONFIG_BRANCH" else default
    )
    return settings


def test_get_repo_settings_reads_default_branch_without_config_branch(project):
    project.files.get.return_value = _file(b"[config]\nmodel='default'")
    provider = _provider_with_project(project)

    with patch("pr_agent.git_providers.git_provider.get_settings", return_value=_settings(None)), \
            patch.dict("os.environ", {}, clear=True):
        settings = provider.get_repo_settings()

    assert settings == [("local", b"[config]\nmodel='default'")]
    project.files.get.assert_called_once_with(file_path=".pr_agent.toml", ref="main")
    assert provider._resolved_config_branch == "main"


def test_get_repo_settings_uses_config_branch_from_settings(project):
    project.files.get.return_value = _file(b"[config]\nmodel='x'")
    provider = _provider_with_project(project)

    with patch("pr_agent.git_providers.git_provider.get_settings", return_value=_settings("feature-config")):
        settings = provider.get_repo_settings()

    assert settings == [("local", b"[config]\nmodel='x'")]
    project.files.get.assert_called_once_with(file_path=".pr_agent.toml", ref="feature-config")
    assert provider._resolved_config_branch == "feature-config"


def test_get_repo_settings_uses_env_var_when_settings_are_missing(project):
    project.files.get.return_value = _file(b"[config]\nmodel='env'")
    provider = _provider_with_project(project)

    with patch("pr_agent.git_providers.git_provider.get_settings", return_value=_settings("   ")), \
            patch.dict("os.environ", {"PR_AGENT_CONFIG_BRANCH": "env-branch"}, clear=False):
        settings = provider.get_repo_settings()

    assert settings == [("local", b"[config]\nmodel='env'")]
    project.files.get.assert_called_once_with(file_path=".pr_agent.toml", ref="env-branch")


def test_get_repo_settings_falls_back_to_default_branch_when_config_branch_has_no_file(project):
    project.files.get.side_effect = [
        GitlabGetError("404 Not Found", response_code=404),
        _file(b"[config]\nmodel='default'"),
    ]
    provider = _provider_with_project(project)

    with patch("pr_agent.git_providers.git_provider.get_settings", return_value=_settings("feature-config")):
        settings = provider.get_repo_settings()

    assert settings == [("local", b"[config]\nmodel='default'")]
    assert [c.kwargs["ref"] for c in project.files.get.call_args_list] == ["feature-config", "main"]
    assert provider._resolved_config_branch == "main"


def test_get_repo_settings_does_not_fall_back_on_non_404_error_for_config_branch(project):
    project.files.get.side_effect = GitlabGetError("403 Forbidden", response_code=403)
    provider = _provider_with_project(project)

    with patch("pr_agent.git_providers.git_provider.get_settings", return_value=_settings("feature-config")), \
            patch("pr_agent.git_providers.gitlab_provider.get_logger") as mock_logger:
        settings = provider.get_repo_settings()

    assert settings == ""
    project.files.get.assert_called_once_with(file_path=".pr_agent.toml", ref="feature-config")
    mock_logger.return_value.warning.assert_called_once()


def test_get_repo_settings_missing_file_on_default_branch_is_quiet(project):
    project.files.get.side_effect = GitlabGetError("404 Not Found", response_code=404)
    provider = _provider_with_project(project)

    with patch("pr_agent.git_providers.git_provider.get_settings", return_value=_settings(None)), \
            patch.dict("os.environ", {}, clear=True), \
            patch("pr_agent.git_providers.gitlab_provider.get_logger") as mock_logger:
        settings = provider.get_repo_settings()

    assert settings == ""
    mock_logger.return_value.warning.assert_not_called()


def test_get_repo_settings_tree_follows_resolved_config_branch(project):
    project.repository_tree.return_value = [
        {"type": "blob", "path": "sub/.pr_agent.toml"},
        {"type": "blob", "path": "README.md"},
    ]
    provider = _provider_with_project(project)
    provider._resolved_config_branch = "feature-config"
    settings = MagicMock()
    settings.config.per_directory_settings_max_tree_pages = 2

    with patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=settings):
        paths, resolved_ref = provider.get_repo_settings_tree("ignored-when-root-resolved")

    assert (paths, resolved_ref) == (["sub/.pr_agent.toml"], "feature-config")
    project.repository_tree.assert_called_once_with(ref="feature-config", recursive=True, page=1, per_page=100)


def test_get_repo_settings_tree_defaults_to_default_branch(project):
    project.repository_tree.return_value = []
    provider = _provider_with_project(project)
    settings = MagicMock()
    settings.config.per_directory_settings_max_tree_pages = 2

    with patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=settings):
        paths, resolved_ref = provider.get_repo_settings_tree()

    assert (paths, resolved_ref) == ([], "main")
    project.repository_tree.assert_called_once_with(ref="main", recursive=True, page=1, per_page=100)


def test_get_repo_settings_tree_retries_default_branch_when_explicit_ref_is_missing(project):
    project.repository_tree.side_effect = [
        GitlabGetError("404 Tree Not Found", response_code=404),
        [{"type": "blob", "path": "sub/.pr_agent.toml"}],
    ]
    provider = _provider_with_project(project)
    settings = MagicMock()
    settings.config.per_directory_settings_max_tree_pages = 2

    with patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=settings):
        paths, resolved_ref = provider.get_repo_settings_tree("deleted-branch")

    assert (paths, resolved_ref) == (["sub/.pr_agent.toml"], "main")
    assert [c.kwargs["ref"] for c in project.repository_tree.call_args_list] == ["deleted-branch", "main"]


def test_get_repo_settings_tree_returns_empty_when_default_branch_tree_is_missing_too(project):
    project.repository_tree.side_effect = GitlabGetError("404 Tree Not Found", response_code=404)
    provider = _provider_with_project(project)
    settings = MagicMock()
    settings.config.per_directory_settings_max_tree_pages = 2

    with patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=settings):
        paths, resolved_ref = provider.get_repo_settings_tree("deleted-branch")

    assert (paths, resolved_ref) == ([], "")
    assert [c.kwargs["ref"] for c in project.repository_tree.call_args_list] == ["deleted-branch", "main"]


def test_get_repo_settings_tree_does_not_retry_on_non_404_error(project):
    project.repository_tree.side_effect = GitlabGetError("403 Forbidden", response_code=403)
    provider = _provider_with_project(project)
    settings = MagicMock()
    settings.config.per_directory_settings_max_tree_pages = 2

    with patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=settings), \
            pytest.raises(GitlabGetError):
        provider.get_repo_settings_tree("feature-config")

    project.repository_tree.assert_called_once()


def test_get_repo_settings_tree_skips_nested_settings_when_resolved_root_branch_vanished(project):
    project.repository_tree.side_effect = GitlabGetError("404 Tree Not Found", response_code=404)
    provider = _provider_with_project(project)
    provider._resolved_config_branch = "feature-config"
    settings = MagicMock()
    settings.config.per_directory_settings_max_tree_pages = 2

    with patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=settings):
        paths, resolved_ref = provider.get_repo_settings_tree("feature-config")

    assert (paths, resolved_ref) == ([], "")
    project.repository_tree.assert_called_once_with(ref="feature-config", recursive=True, page=1, per_page=100)
