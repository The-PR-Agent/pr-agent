from unittest.mock import MagicMock

import pytest
from github import GithubException

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import git_provider as _gp
from pr_agent.git_providers.github_provider import GithubProvider


@pytest.fixture(autouse=True)
def _clear_global_settings_cache():
    # The org global-settings cache is process-level; clear it between tests to avoid pollution.
    _gp._GLOBAL_SETTINGS_CACHE.clear()
    yield
    _gp._GLOBAL_SETTINGS_CACHE.clear()


def _not_found(name):
    # Match PyGithub: a missing repo/file raises GithubException(404), which the provider
    # handles, rather than FileNotFoundError.
    return GithubException(404, {"message": f"Not Found: {name}"}, {})


class FakeContent:
    def __init__(self, decoded_content):
        self.decoded_content = decoded_content


class FakeRepo:
    def __init__(self, files=None):
        self.files = files or {}

    def get_contents(self, path, ref=None):
        if path not in self.files:
            raise _not_found(path)
        return FakeContent(self.files[path])


class FakeGithubClient:
    def __init__(self, repos=None):
        self.repos = repos or {}

    def get_repo(self, repo_name):
        if repo_name not in self.repos:
            raise _not_found(repo_name)
        return self.repos[repo_name]


def _provider(local_settings=None, global_settings=None):
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "org/service"
    provider.repo_obj = FakeRepo({".pr_agent.toml": local_settings} if local_settings is not None else {})
    provider.github_client = FakeGithubClient(
        {"org/pr-agent-settings": FakeRepo({".pr_agent.toml": global_settings})}
        if global_settings is not None
        else {}
    )
    return provider


def test_get_global_repo_settings_repo_less_provider_does_not_crash():
    # A provider built via __new__ (no repo/github_client) must not raise from _get_global_repo_settings.
    provider = GithubProvider.__new__(GithubProvider)
    settings = get_settings()
    original = settings.config.use_global_settings_file
    settings.config.use_global_settings_file = True
    try:
        assert provider._get_global_repo_settings() == ""
    finally:
        settings.config.use_global_settings_file = original


def test_get_repo_settings_missing_local_file_logged_quietly(monkeypatch):
    # A missing local .pr_agent.toml (404) is expected for most repos; it must be logged at debug,
    # not warning.
    from unittest.mock import patch

    provider = _provider()  # no local .pr_agent.toml -> get_contents raises GithubException(404)
    settings = get_settings()
    original = settings.config.use_global_settings_file
    original_branch = settings.get("CONFIG.CONFIG_BRANCH", None)
    settings.config.use_global_settings_file = False  # isolate the local-load path
    monkeypatch.delenv("PR_AGENT_CONFIG_BRANCH", raising=False)
    settings.set("CONFIG.CONFIG_BRANCH", "")  # ensure the default-branch load path runs
    try:
        with patch("pr_agent.git_providers.github_provider.get_logger") as mock_get_logger:
            logger = mock_get_logger.return_value
            result = provider.get_repo_settings()

        assert result == ""
        logger.warning.assert_not_called()
    finally:
        settings.config.use_global_settings_file = original
        settings.set("CONFIG.CONFIG_BRANCH", original_branch)


def test_get_global_repo_settings_missing_repo_logged_quietly():
    # A missing/inaccessible <owner>/pr-agent-settings repo is an expected fallback, so it must be
    # logged quietly (debug), not as a warning that would flood logs on every webhook event.
    from unittest.mock import patch

    provider = _provider()  # no global settings repo -> get_repo raises GithubException(404)
    settings = get_settings()
    original = settings.config.use_global_settings_file
    settings.config.use_global_settings_file = True
    try:
        with patch("pr_agent.git_providers.github_provider.get_logger") as mock_get_logger:
            logger = mock_get_logger.return_value
            result = provider._get_global_repo_settings()

        assert result == ""
        logger.warning.assert_not_called()
        logger.debug.assert_called_once()
    finally:
        settings.config.use_global_settings_file = original


def test_get_repo_settings_returns_global_settings_when_local_settings_missing():
    provider = _provider(global_settings=b"[pr_reviewer]\nextra_instructions = \"global\"\n")

    settings = provider.get_repo_settings()

    assert settings == [("global", b"[pr_reviewer]\nextra_instructions = \"global\"\n")]


def test_get_repo_settings_merges_global_before_local_settings():
    provider = _provider(
        global_settings=b"[pr_reviewer]\nextra_instructions = \"global\"\n",
        local_settings=b"[pr_description]\npublish_labels = false\n",
    )

    settings = provider.get_repo_settings()

    assert settings == [
        ("global", b"[pr_reviewer]\nextra_instructions = \"global\"\n"),
        ("local", b"[pr_description]\npublish_labels = false\n"),
    ]


def test_get_repo_settings_keeps_global_and_local_same_section_separate():
    provider = _provider(
        global_settings=b"[pr_reviewer]\nextra_instructions = \"global\"\nnum_code_suggestions = 3\n",
        local_settings=b"[pr_reviewer]\nextra_instructions = \"local\"\n",
    )

    settings = provider.get_repo_settings()

    assert settings == [
        ("global", b"[pr_reviewer]\nextra_instructions = \"global\"\nnum_code_suggestions = 3\n"),
        ("local", b"[pr_reviewer]\nextra_instructions = \"local\"\n"),
    ]


def test_get_repo_settings_skips_global_settings_when_disabled():
    settings = get_settings()
    original = settings.config.use_global_settings_file
    settings.config.use_global_settings_file = False
    try:
        provider = _provider(
            global_settings=b"[pr_reviewer]\nextra_instructions = \"global\"\n",
            local_settings=b"[pr_reviewer]\nextra_instructions = \"local\"\n",
        )

        repo_settings = provider.get_repo_settings()

        assert repo_settings == [("local", b"[pr_reviewer]\nextra_instructions = \"local\"\n")]
    finally:
        settings.config.use_global_settings_file = original


def test_get_repo_settings_config_branch_non_404_error_propagates(monkeypatch):
    # A non-404 error loading the config branch must NOT be masked by a silent default-branch fallback.
    provider = _provider()  # no global settings repo -> _get_global returns "" quietly
    provider.repo_obj = MagicMock()
    provider.repo_obj.get_contents.side_effect = GithubException(403, {"message": "Forbidden"}, {})
    monkeypatch.setenv("PR_AGENT_CONFIG_BRANCH", "release")
    settings = get_settings()
    original = settings.config.use_global_settings_file
    settings.config.use_global_settings_file = False
    try:
        import pytest as _pytest
        with _pytest.raises(GithubException):
            provider.get_repo_settings()
    finally:
        settings.config.use_global_settings_file = original
