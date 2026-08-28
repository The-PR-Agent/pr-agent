from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from github import GithubException

from pr_agent.git_providers.github_provider import GithubProvider


def _provider_with_repo(repo_obj):
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo_obj = repo_obj
    return provider


def test_get_repo_settings_uses_config_branch_from_settings():
    repo_obj = MagicMock()
    repo_obj.get_contents.return_value = SimpleNamespace(decoded_content=b"[config]\nmodel='x'")
    provider = _provider_with_repo(repo_obj)

    with patch("pr_agent.git_providers.github_provider.get_settings") as mock_settings:
        mock_settings.return_value.get.return_value = "feature-config"
        settings = provider.get_repo_settings()

    assert settings == b"[config]\nmodel='x'"
    repo_obj.get_contents.assert_called_once_with(".pr_agent.toml", ref="feature-config")


def test_get_repo_settings_falls_back_to_default_branch_on_missing_file_in_config_branch():
    repo_obj = MagicMock()
    repo_obj.get_contents.side_effect = [
        GithubException(404, {"message": "Not Found"}, None),
        SimpleNamespace(decoded_content=b"[config]\nmodel='default'"),
    ]
    provider = _provider_with_repo(repo_obj)

    with patch("pr_agent.git_providers.github_provider.get_settings") as mock_settings:
        mock_settings.return_value.get.return_value = "feature-config"
        settings = provider.get_repo_settings()

    assert settings == b"[config]\nmodel='default'"
    assert repo_obj.get_contents.call_args_list[0].kwargs == {"ref": "feature-config"}
    assert repo_obj.get_contents.call_args_list[1].kwargs == {}


def test_get_repo_settings_uses_env_var_when_settings_are_missing():
    repo_obj = MagicMock()
    repo_obj.get_contents.return_value = SimpleNamespace(decoded_content=b"[config]\nmodel='env'")
    provider = _provider_with_repo(repo_obj)

    with patch("pr_agent.git_providers.github_provider.get_settings") as mock_settings, patch.dict(
        "os.environ",
        {"PR_AGENT_CONFIG_BRANCH": "env-branch"},
        clear=False,
    ):
        mock_settings.return_value.get.return_value = None
        settings = provider.get_repo_settings()

    assert settings == b"[config]\nmodel='env'"
    repo_obj.get_contents.assert_called_once_with(".pr_agent.toml", ref="env-branch")


def test_get_repo_settings_whitespace_settings_falls_back_to_env_var():
    """A whitespace-only CONFIG.CONFIG_BRANCH must not short-circuit the env fallback."""
    repo_obj = MagicMock()
    repo_obj.get_contents.return_value = SimpleNamespace(decoded_content=b"[config]\nmodel='env'")
    provider = _provider_with_repo(repo_obj)

    with patch("pr_agent.git_providers.github_provider.get_settings") as mock_settings, patch.dict(
        "os.environ",
        {"PR_AGENT_CONFIG_BRANCH": "env-branch"},
        clear=False,
    ):
        mock_settings.return_value.get.return_value = "   "
        settings = provider.get_repo_settings()

    assert settings == b"[config]\nmodel='env'"
    repo_obj.get_contents.assert_called_once_with(".pr_agent.toml", ref="env-branch")
