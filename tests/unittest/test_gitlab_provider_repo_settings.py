from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from gitlab.exceptions import GitlabGetError

from pr_agent.git_providers.gitlab_provider import GitLabProvider

SHA_A = "a" * 40
SHA_B = "b" * 40


def _file(content: bytes, commit_id=SHA_A):
    return SimpleNamespace(decode=lambda: content, commit_id=commit_id, last_commit_id="c" * 40)


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
    project.commits.get.side_effect = lambda ref: SimpleNamespace(id=SHA_A if ref == "main" else SHA_B)
    project.files.get.side_effect = GitlabGetError(response_code=404)
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
    project.files.get.side_effect = None
    project.files.get.return_value = _file(b"[config]\nmodel='default'")
    provider = _provider_with_project(project)

    with patch("pr_agent.git_providers.git_provider.get_settings", return_value=_settings(None)), \
            patch.dict("os.environ", {}, clear=True):
        settings = provider.get_repo_settings()

    assert settings == [("local", b"[config]\nmodel='default'")]
    project.files.get.assert_called_once_with(file_path=".pr_agent.toml", ref="main")
    assert provider._resolved_config_branch == SHA_A


def test_get_repo_settings_uses_config_branch_from_settings(project):
    project.files.get.side_effect = None
    project.files.get.return_value = _file(b"[config]\nmodel='x'")
    provider = _provider_with_project(project)

    with patch("pr_agent.git_providers.git_provider.get_settings", return_value=_settings("feature-config")):
        settings = provider.get_repo_settings()

    assert settings == [("local", b"[config]\nmodel='x'")]
    project.files.get.assert_called_once_with(file_path=".pr_agent.toml", ref="feature-config")
    assert provider._resolved_config_branch == SHA_A


def test_get_repo_settings_uses_env_var_when_settings_are_missing(project):
    project.files.get.side_effect = None
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
    assert provider._resolved_config_branch == SHA_A


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
    provider._resolved_config_branch = SHA_B
    settings = MagicMock()
    settings.config.per_directory_settings_max_tree_pages = 2

    with patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=settings):
        paths, resolved_ref = provider.get_repo_settings_tree("ignored-when-root-resolved")

    assert (paths, resolved_ref) == (["sub/.pr_agent.toml"], SHA_B)
    project.repository_tree.assert_called_once_with(ref=SHA_B, recursive=True, page=1, per_page=100)


def test_get_repo_settings_tree_defaults_to_default_branch(project):
    project.repository_tree.return_value = []
    provider = _provider_with_project(project)
    settings = MagicMock()
    settings.config.per_directory_settings_max_tree_pages = 2

    with patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=settings):
        paths, resolved_ref = provider.get_repo_settings_tree()

    assert (paths, resolved_ref) == ([], SHA_A)
    project.repository_tree.assert_called_once_with(ref=SHA_A, recursive=True, page=1, per_page=100)


@pytest.mark.parametrize("root_resolved", [False, True])
def test_get_repo_settings_tree_reuses_complete_paths(project, root_resolved):
    provider = _provider_with_project(project)
    if root_resolved:
        provider._resolved_config_branch = SHA_A
    first_page = [{"type": "blob", "path": "README.md"}] * 99 + [
        {"type": "blob", "path": "sub/.pr_agent.toml"}]
    project.repository_tree.side_effect = lambda **kwargs: first_page if kwargs["page"] == 1 else []
    settings = MagicMock()
    settings.config.per_directory_settings_max_tree_pages = 2

    with patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=settings):
        paths, ref = provider.get_repo_settings_tree()
        assert (paths, ref) == (["sub/.pr_agent.toml"], SHA_A)
        paths.clear()
        assert provider.get_repo_settings_tree() == (["sub/.pr_agent.toml"], SHA_A)
        assert provider.get_repo_settings_tree() == (["sub/.pr_agent.toml"], SHA_A)
        provider._repo_settings_failed = True
        assert provider.get_repo_settings_tree() == ([], "")

    assert project.repository_tree.call_args_list == [
        call(ref=SHA_A, recursive=True, page=1, per_page=100),
        call(ref=SHA_A, recursive=True, page=2, per_page=100),
    ]


@pytest.mark.parametrize(("next_ref", "next_limit", "expected_calls"), [
    (SHA_B, 1, 2), (SHA_A, 2, 2), (SHA_A, True, 1),
])
def test_get_repo_settings_tree_cache_respects_revision_and_page_limit(project, next_ref, next_limit, expected_calls):
    provider = _provider_with_project(project)
    provider._resolved_config_branch = SHA_A
    project.repository_tree.return_value = [{"type": "blob", "path": "sub/.pr_agent.toml"}]
    settings = MagicMock()
    settings.config.per_directory_settings_max_tree_pages = 1

    with patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=settings):
        assert provider.get_repo_settings_tree() == (["sub/.pr_agent.toml"], SHA_A)
        assert provider.get_repo_settings_tree() == (["sub/.pr_agent.toml"], SHA_A)
        assert project.repository_tree.call_count == 1
        provider._resolved_config_branch = next_ref
        settings.config.per_directory_settings_max_tree_pages = next_limit
        expected_paths = [] if isinstance(next_limit, bool) else ["sub/.pr_agent.toml"]
        assert provider.get_repo_settings_tree() == (expected_paths, next_ref)

    assert project.repository_tree.call_count == expected_calls


@pytest.mark.parametrize("first_page_size", [0, 100])
def test_get_repo_settings_tree_retries_empty_or_incomplete_discovery(project, first_page_size):
    provider = _provider_with_project(project)
    provider._resolved_config_branch = SHA_A
    complete_page = [{"type": "blob", "path": "sub/.pr_agent.toml"}]
    project.repository_tree.side_effect = [complete_page * first_page_size, complete_page]
    settings = MagicMock()
    settings.config.per_directory_settings_max_tree_pages = 1

    with patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=settings):
        assert provider.get_repo_settings_tree() == ([], SHA_A)
        assert provider.get_repo_settings_tree() == (["sub/.pr_agent.toml"], SHA_A)
        assert provider.get_repo_settings_tree() == (["sub/.pr_agent.toml"], SHA_A)

    assert project.repository_tree.call_count == 2


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

    assert (paths, resolved_ref) == (["sub/.pr_agent.toml"], SHA_A)
    assert [c.kwargs["ref"] for c in project.repository_tree.call_args_list] == [SHA_B, SHA_A]


def test_get_repo_settings_tree_returns_empty_when_default_branch_tree_is_missing_too(project):
    project.repository_tree.side_effect = GitlabGetError("404 Tree Not Found", response_code=404)
    provider = _provider_with_project(project)
    settings = MagicMock()
    settings.config.per_directory_settings_max_tree_pages = 2

    with patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=settings):
        paths, resolved_ref = provider.get_repo_settings_tree("deleted-branch")

    assert (paths, resolved_ref) == ([], "")
    assert [c.kwargs["ref"] for c in project.repository_tree.call_args_list] == [SHA_B, SHA_A]


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
    provider._resolved_config_branch = SHA_B
    settings = MagicMock()
    settings.config.per_directory_settings_max_tree_pages = 2

    with patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=settings):
        paths, resolved_ref = provider.get_repo_settings_tree("feature-config")

    assert (paths, resolved_ref) == ([], "")
    project.repository_tree.assert_called_once_with(ref=SHA_B, recursive=True, page=1, per_page=100)


@pytest.mark.parametrize("root", [b"", b"[config]\nmodel='root'"])
def test_root_bytes_pin_tree_pages_and_nested_content_without_resolving_again(project, root):
    provider = _provider_with_project(project)
    project.files.get.side_effect = [_file(root), _file(b"nested", SHA_B)]
    project.repository_tree.side_effect = [
        [{"type": "blob", "path": "readme"}] * 99 + [{"type": "blob", "path": "sub/.pr_agent.toml"}], []]
    settings = MagicMock()
    settings.config.per_directory_settings_max_tree_pages = 3
    with patch("pr_agent.git_providers.git_provider.get_settings", return_value=_settings("release")), \
            patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=settings):
        assert provider.get_repo_settings() == ([("local", root)] if root else "")
        paths, ref = provider.get_repo_settings_tree("release")
        assert provider.get_repo_settings_contents(paths, ref) == {"sub/.pr_agent.toml": b"nested"}
    assert ref == SHA_A
    assert project.repository_tree.call_args_list == [
        call(ref=SHA_A, recursive=True, page=1, per_page=100),
        call(ref=SHA_A, recursive=True, page=2, per_page=100)]
    assert project.files.get.call_args_list == [
        call(file_path=".pr_agent.toml", ref="release"), call(file_path="sub/.pr_agent.toml", ref=SHA_A)]
    project.commits.get.assert_not_called()


@pytest.mark.parametrize("ref", ["release", "v1.0", SHA_B])
def test_rootless_acquisition_rechecks_absence_at_selected_commit(project, ref):
    provider = _provider_with_project(project)
    project.repository_tree.return_value = [{"type": "blob", "path": "sub/.pr_agent.toml"}]
    settings = MagicMock()
    settings.config.per_directory_settings_max_tree_pages = 2
    with patch("pr_agent.git_providers.git_provider.get_settings", return_value=_settings(ref)), \
            patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=settings):
        assert provider.get_repo_settings() == ""
        project.commits.get.assert_not_called()  # No enabled per-directory request yet.
        paths, selected = provider.get_repo_settings_tree(ref)
        project.files.get.side_effect = None
        project.files.get.return_value = _file(b"nested")
        assert provider.get_repo_settings_contents(paths, selected) == {"sub/.pr_agent.toml": b"nested"}
    assert selected == SHA_B
    project.commits.get.assert_called_once_with(ref)
    assert project.files.get.call_args_list == [call(file_path=".pr_agent.toml", ref=ref),
        call(file_path=".pr_agent.toml", ref="main"), call(file_path=".pr_agent.toml", ref=SHA_B),
        call(file_path="sub/.pr_agent.toml", ref=SHA_B)]


@pytest.mark.parametrize("root", [b"", b"[config]\nmodel='new-root'"])
def test_root_appearing_during_rootless_resolution_skips_nested_without_fallback(project, root):
    provider = _provider_with_project(project)
    project.files.get.side_effect = [GitlabGetError(response_code=404), GitlabGetError(response_code=404), _file(root)]
    with patch("pr_agent.git_providers.git_provider.get_settings", return_value=_settings("feature")):
        assert provider.get_repo_settings() == ""
        assert provider.get_repo_settings_tree("feature") == ([], "")
    project.repository_tree.assert_not_called()
    project.commits.get.assert_called_once_with("feature")


@pytest.mark.parametrize("where", ["resolve", "recheck"])
@pytest.mark.parametrize("status", [403, 500])
def test_rootless_non_404_never_falls_back(project, where, status):
    provider = _provider_with_project(project)
    if where == "resolve":
        project.commits.get.side_effect = GitlabGetError(response_code=status)
    else:
        project.files.get.side_effect = GitlabGetError(response_code=status)
    with pytest.raises(GitlabGetError):
        provider.get_repo_settings_tree("feature")
    project.repository_tree.assert_not_called()
    project.commits.get.assert_called_once_with("feature")


def test_rootless_missing_commit_can_fall_back_to_default(project):
    project.commits.get.side_effect = [GitlabGetError(response_code=404), SimpleNamespace(id=SHA_A)]
    project.repository_tree.return_value = []
    settings = MagicMock()
    settings.config.per_directory_settings_max_tree_pages = 2
    with patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=settings):
        assert _provider_with_project(project).get_repo_settings_tree("missing") == ([], SHA_A)
    assert project.commits.get.call_args_list == [call("missing"), call("main")]
    project.files.get.assert_called_once_with(file_path=".pr_agent.toml", ref=SHA_A)


@pytest.mark.parametrize("second", [
    GitlabGetError(response_code=403), GitlabGetError(response_code=404), _file(b"new", SHA_B)])
def test_new_acquisition_clears_previous_selection_and_error_state(project, second):
    provider = _provider_with_project(project)
    project.files.get.side_effect = [_file(b"old"), second]
    with patch("pr_agent.git_providers.git_provider.get_settings", return_value=_settings(None)), \
            patch.dict("os.environ", {}, clear=True):
        assert provider.get_repo_settings() == [("local", b"old")]
        provider.get_repo_settings()
    assert provider._resolved_config_branch == (SHA_B if not isinstance(second, Exception) else "")
    if isinstance(second, GitlabGetError) and second.response_code != 404:
        assert provider.get_repo_settings_tree() == ([], "")
        project.commits.get.assert_not_called()
        project.repository_tree.assert_not_called()
    project.files.get.side_effect = None
    project.files.get.return_value = _file(b"recovered", SHA_A)
    with patch("pr_agent.git_providers.git_provider.get_settings", return_value=_settings(None)):
        assert provider.get_repo_settings() == [("local", b"recovered")]
    assert provider._resolved_config_branch == SHA_A


def test_acquisition_resets_snapshot_before_global_settings_failure(project):
    provider = _provider_with_project(project)
    provider._resolved_config_branch = SHA_A
    with patch.object(GitLabProvider, "_get_global_repo_settings", side_effect=RuntimeError("global failure")), \
            pytest.raises(RuntimeError, match="global failure"):
        provider.get_repo_settings()
    assert provider.get_repo_settings_tree() == ([], "")
    project.repository_tree.assert_not_called()
