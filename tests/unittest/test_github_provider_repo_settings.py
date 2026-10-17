from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from github import GithubException
from github.Commit import Commit

from pr_agent.git_providers.github_provider import GithubProvider


def _repo():
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_commit.side_effect = lambda ref: SimpleNamespace(
        sha=f"{ref}-commit", commit=SimpleNamespace(tree=SimpleNamespace(sha=f"{ref}-tree")), complete=lambda: None,
    )
    repo.get_git_tree.return_value = SimpleNamespace(tree=[], truncated=False)
    return repo


def _provider_with_repo(repo_obj):
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo_obj = repo_obj
    return provider


@pytest.fixture(autouse=True)
def no_global_settings():
    # Patch get_settings in git_provider, where get_config_branch() lives, and keep the
    # namespace-wide lookup out of the way so that patch does not leak into it.
    with patch.object(GithubProvider, "_get_global_repo_settings", return_value=""):
        yield


def test_get_repo_settings_uses_config_branch_from_settings():
    repo_obj = _repo()
    repo_obj.get_contents.return_value = SimpleNamespace(decoded_content=b"[config]\nmodel='x'")
    provider = _provider_with_repo(repo_obj)

    with patch("pr_agent.git_providers.git_provider.get_settings") as mock_settings:
        mock_settings.return_value.get.return_value = "feature-config"
        settings = provider.get_repo_settings()

    assert settings == b"[config]\nmodel='x'"
    repo_obj.get_contents.assert_called_once_with(".pr_agent.toml", ref="feature-config-commit")


def test_get_repo_settings_falls_back_to_default_branch_on_missing_file_in_config_branch():
    repo_obj = _repo()
    repo_obj.get_contents.side_effect = [
        GithubException(404, {"message": "Not Found"}, None),
        SimpleNamespace(decoded_content=b"[config]\nmodel='default'"),
    ]
    provider = _provider_with_repo(repo_obj)

    with patch("pr_agent.git_providers.git_provider.get_settings") as mock_settings:
        mock_settings.return_value.get.return_value = "feature-config"
        settings = provider.get_repo_settings()

    assert settings == b"[config]\nmodel='default'"
    assert repo_obj.get_contents.call_args_list[0].kwargs == {"ref": "feature-config-commit"}
    assert repo_obj.get_contents.call_args_list[1].kwargs == {"ref": "main-commit"}


def test_get_repo_settings_uses_env_var_when_settings_are_missing():
    repo_obj = _repo()
    repo_obj.get_contents.return_value = SimpleNamespace(decoded_content=b"[config]\nmodel='env'")
    provider = _provider_with_repo(repo_obj)

    with patch("pr_agent.git_providers.git_provider.get_settings") as mock_settings, patch.dict(
        "os.environ",
        {"PR_AGENT_CONFIG_BRANCH": "env-branch"},
        clear=False,
    ):
        mock_settings.return_value.get.return_value = None
        settings = provider.get_repo_settings()

    assert settings == b"[config]\nmodel='env'"
    repo_obj.get_contents.assert_called_once_with(".pr_agent.toml", ref="env-branch-commit")


def test_get_repo_settings_whitespace_settings_falls_back_to_env_var():
    """A whitespace-only CONFIG.CONFIG_BRANCH must not short-circuit the env fallback."""
    repo_obj = _repo()
    repo_obj.get_contents.return_value = SimpleNamespace(decoded_content=b"[config]\nmodel='env'")
    provider = _provider_with_repo(repo_obj)

    with patch("pr_agent.git_providers.git_provider.get_settings") as mock_settings, patch.dict(
        "os.environ",
        {"PR_AGENT_CONFIG_BRANCH": "env-branch"},
        clear=False,
    ):
        mock_settings.return_value.get.return_value = "   "
        settings = provider.get_repo_settings()

    assert settings == b"[config]\nmodel='env'"
    repo_obj.get_contents.assert_called_once_with(".pr_agent.toml", ref="env-branch-commit")


@pytest.mark.parametrize("config_ref, missing_ref, root_ref, selected", [
    ("config", None, "config-commit", "config"),
    ("config", None, "main-commit", "main"),
    ("config", None, None, "config"),
    ("config", "config", None, "main"),
    ("", None, None, "main"),
    ("main", None, None, "main"),
])
def test_settings_selection_pins_root_absence_and_nested_files(config_ref, missing_ref, root_ref, selected):
    repo = _repo()
    resolve = repo.get_commit.side_effect

    def get_commit(ref):
        if ref == missing_ref:
            raise GithubException(404, {}, None)
        return resolve(ref)

    def get_contents(path, ref):
        if path == ".pr_agent.toml" and ref != root_ref:
            raise GithubException(404, {}, None)
        return SimpleNamespace(decoded_content=b"")

    repo.get_commit.side_effect = get_commit
    repo.get_contents.side_effect = get_contents
    provider = _provider_with_repo(repo)
    with patch("pr_agent.git_providers.github_provider.get_config_branch", return_value=config_ref):
        provider.get_repo_settings()
    _, ref = provider.get_repo_settings_tree(config_ref)
    assert ref == f"{selected}-commit"
    repo.get_git_tree.assert_called_once_with(f"{selected}-tree", recursive=True)
    assert provider.get_repo_settings_contents(["svc/.pr_agent.toml"], ref) == {"svc/.pr_agent.toml": b""}
    assert repo.get_contents.call_args == call("svc/.pr_agent.toml", ref=f"{selected}-commit")
    attempted = [c.args[0] for c in repo.get_commit.call_args_list]
    assert len(attempted) == len(set(attempted)) <= 2


def test_settings_snapshot_hydrates_lazy_sdk_commit_before_reading_sha():
    requester = MagicMock()
    requester.is_not_lazy = False
    requester.requestJsonAndCheck.return_value = ({}, {
        "sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}},
    })
    commit = Commit(requester, {}, {"url": "https://api.github.com/repos/org/repo/commits/config"}, completed=False)
    assert commit.sha == "config"
    repo = _repo()
    repo.get_commit.side_effect = None
    repo.get_commit.return_value = commit
    repo.get_contents.return_value = SimpleNamespace(decoded_content=b"[config]")
    provider = _provider_with_repo(repo)
    with patch("pr_agent.git_providers.github_provider.get_config_branch", return_value="config"):
        provider.get_repo_settings()
    assert provider.get_repo_settings_tree()[1] == "a" * 40
    repo.get_contents.assert_called_once_with(".pr_agent.toml", ref="a" * 40)
    repo.get_git_tree.assert_called_once_with("b" * 40, recursive=True)
    requester.requestJsonAndCheck.assert_called_once()


def test_captured_root_tree_404_does_not_mix_default_settings():
    repo = _repo()
    repo.get_contents.return_value = SimpleNamespace(decoded_content=b"[config]")
    repo.get_git_tree.side_effect = GithubException(404, {}, None)
    provider = _provider_with_repo(repo)
    with patch("pr_agent.git_providers.github_provider.get_config_branch", return_value="config"):
        assert provider.get_repo_settings() == b"[config]"
    assert provider.get_repo_settings_tree("main") == ([], "config-commit")
    repo.get_commit.assert_called_once_with("config")
    repo.get_git_tree.assert_called_once_with("config-tree", recursive=True)


@pytest.mark.parametrize("failing_method", ["get_commit", "get_contents", "global"])
def test_new_acquisition_clears_old_snapshot_before_failure(failing_method):
    repo = _repo()
    repo.get_contents.return_value = SimpleNamespace(decoded_content=b"[config]")
    provider = _provider_with_repo(repo)
    with patch("pr_agent.git_providers.github_provider.get_config_branch", return_value=""):
        provider.get_repo_settings()
        if failing_method == "global":
            with patch.object(provider, "_get_global_repo_settings", side_effect=GithubException(503, {}, None)):
                with pytest.raises(GithubException):
                    provider.get_repo_settings()
        else:
            getattr(repo, failing_method).side_effect = GithubException(503, {}, None)
            provider.get_repo_settings()
    assert provider.get_repo_settings_tree() == ([], "")
    repo.get_git_tree.assert_not_called()
