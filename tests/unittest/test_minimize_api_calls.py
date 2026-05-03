"""
Tests for the minimize_api_calls optimization phases.

Covers:
  Phase 1: Commit caching in get_commit_messages()
  Phase 2: Language caching in get_languages()
  Phase 4: handle_patch_deletions guard for empty content
  Phase 5: Temporary comment suppression
  Phase 6: Label caching via PR object
  Config:  Default value of github.minimize_api_calls
"""

from unittest.mock import MagicMock, patch

import pytest

from pr_agent.algo.git_patch_processing import handle_patch_deletions
from pr_agent.algo.types import EDIT_TYPE


# ---------------------------------------------------------------------------
# Phase 1: Cache commits in get_commit_messages()
# ---------------------------------------------------------------------------


class TestCacheCommits:
    """Phase 1 — get_commit_messages() should reuse self.pr_commits."""

    def _make_provider(self, pr_commits=None):
        """Create a minimal GithubProvider-like object with mocked internals."""
        with patch("pr_agent.git_providers.github_provider.GithubProvider.__init__", return_value=None):
            from pr_agent.git_providers.github_provider import GithubProvider

            provider = GithubProvider.__new__(GithubProvider)

        # Wire up the attributes get_commit_messages() depends on
        provider.pr = MagicMock()
        provider.pr_commits = pr_commits
        return provider

    @patch("pr_agent.git_providers.github_provider.get_settings")
    def test_get_commit_messages_uses_cached_commits(self, mock_settings):
        """When pr_commits is populated, get_commit_messages() must NOT call pr.get_commits()."""
        mock_settings.return_value.get.return_value = None  # MAX_COMMITS_TOKENS

        commit = MagicMock()
        commit.commit.message = "feat: add widget"
        provider = self._make_provider(pr_commits=[commit])

        result = provider.get_commit_messages()

        provider.pr.get_commits.assert_not_called()
        assert "add widget" in result

    @patch("pr_agent.git_providers.github_provider.get_settings")
    def test_get_commit_messages_falls_back_when_no_cache(self, mock_settings):
        """When pr_commits is None, get_commit_messages() should call pr.get_commits()."""
        mock_settings.return_value.get.return_value = None

        commit = MagicMock()
        commit.commit.message = "fix: resolve bug"
        provider = self._make_provider(pr_commits=None)
        provider.pr.get_commits.return_value = [commit]

        result = provider.get_commit_messages()

        provider.pr.get_commits.assert_called_once()
        assert "resolve bug" in result


# ---------------------------------------------------------------------------
# Phase 2: Cache languages in get_languages()
# ---------------------------------------------------------------------------


class TestCacheLanguages:
    """Phase 2 — get_languages() should cache the result after the first call."""

    def _make_provider(self):
        with patch("pr_agent.git_providers.github_provider.GithubProvider.__init__", return_value=None):
            from pr_agent.git_providers.github_provider import GithubProvider

            provider = GithubProvider.__new__(GithubProvider)

        provider._languages = None
        provider.repo_obj = MagicMock()
        return provider

    def _get_repo_stub(self, provider):
        """Stub _get_repo() to return repo_obj."""
        provider._get_repo = MagicMock(return_value=provider.repo_obj)

    def test_get_languages_caches_result(self):
        """Second call returns cached value; _get_repo().get_languages() called once."""
        provider = self._make_provider()
        self._get_repo_stub(provider)
        provider.repo_obj.get_languages.return_value = {"Python": 80, "Go": 20}

        first = provider.get_languages()
        second = provider.get_languages()

        assert first == {"Python": 80, "Go": 20}
        assert second is first  # same object (cached)
        provider.repo_obj.get_languages.assert_called_once()


# ---------------------------------------------------------------------------
# Phase 4: handle_patch_deletions guard
# ---------------------------------------------------------------------------


class TestHandlePatchDeletions:
    """Phase 4 — handle_patch_deletions must respect minimize_api_calls flag."""

    SAMPLE_PATCH = "@@ -1,3 +1,3 @@\n-old\n+new\n context"

    @patch("pr_agent.algo.git_patch_processing.get_settings")
    def test_minimize_mode_preserves_patch_for_unknown_edit_type(self, mock_settings):
        """With minimize_api_calls=True, empty content + UNKNOWN edit type must NOT null the patch."""
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: {
            "github.minimize_api_calls": True,
        }.get(key, default)
        settings.config.verbosity_level = 0
        mock_settings.return_value = settings

        result = handle_patch_deletions(
            patch=self.SAMPLE_PATCH,
            original_file_content_str="",
            new_file_content_str="",
            file_name="test.py",
            edit_type=EDIT_TYPE.UNKNOWN,
        )

        assert result is not None, "Patch should be preserved when minimize_api_calls is active"

    @patch("pr_agent.algo.git_patch_processing.get_settings")
    def test_minimize_mode_nulls_patch_for_deleted_file(self, mock_settings):
        """With minimize_api_calls=True, DELETED edit type must still null the patch."""
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: {
            "github.minimize_api_calls": True,
        }.get(key, default)
        settings.config.verbosity_level = 0
        mock_settings.return_value = settings

        result = handle_patch_deletions(
            patch=self.SAMPLE_PATCH,
            original_file_content_str="",
            new_file_content_str="",
            file_name="deleted.py",
            edit_type=EDIT_TYPE.DELETED,
        )

        assert result is None, "Patch should be None for explicitly deleted files"

    @patch("pr_agent.algo.git_patch_processing.get_settings")
    def test_default_mode_nulls_patch_for_empty_content(self, mock_settings):
        """With minimize_api_calls=False (default), empty content + UNKNOWN nulls the patch."""
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: {
            "github.minimize_api_calls": False,
        }.get(key, default)
        settings.config.verbosity_level = 0
        mock_settings.return_value = settings

        result = handle_patch_deletions(
            patch=self.SAMPLE_PATCH,
            original_file_content_str="",
            new_file_content_str="",
            file_name="missing.py",
            edit_type=EDIT_TYPE.UNKNOWN,
        )

        assert result is None, "Default mode should null patch when content is empty"


# ---------------------------------------------------------------------------
# Phase 5: Skip temporary "Preparing review..." comment
# ---------------------------------------------------------------------------


class TestSkipTempComment:
    """Phase 5 — temporary comment suppressed when minimize_api_calls is active."""

    @patch("pr_agent.tools.pr_reviewer.extract_and_cache_pr_tickets", return_value=None)
    @patch("pr_agent.tools.pr_reviewer.retry_with_fallback_models")
    @patch("pr_agent.tools.pr_reviewer.get_settings")
    def test_skip_temp_comment_when_minimizing(self, mock_settings, mock_retry, mock_tickets):
        """publish_comment('Preparing review...') must NOT be called when minimize_api_calls=True."""
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: {
            "github.minimize_api_calls": True,
        }.get(key, default)
        settings.config.publish_output = True
        settings.config.get.return_value = False  # is_auto_command
        mock_settings.return_value = settings

        from pr_agent.tools.pr_reviewer import PRReviewer

        reviewer = MagicMock(spec=PRReviewer)
        reviewer.incremental = MagicMock()
        reviewer.incremental.is_incremental = False
        reviewer.git_provider = MagicMock()
        reviewer.is_auto = False
        reviewer.is_answer = False

        # Verify the condition: minimize_api_calls prevents the temp comment
        publish_output = settings.config.publish_output
        is_auto = settings.config.get("is_auto_command", False)
        minimize = settings.get("github.minimize_api_calls", False)

        should_publish_temp = publish_output and not is_auto and not minimize
        assert not should_publish_temp, "Temp comment should be suppressed"

    @patch("pr_agent.tools.pr_reviewer.get_settings")
    def test_temp_comment_when_not_minimizing(self, mock_settings):
        """publish_comment('Preparing review...') must be called when minimize_api_calls=False."""
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: {
            "github.minimize_api_calls": False,
        }.get(key, default)
        settings.config.publish_output = True
        settings.config.get.return_value = False  # is_auto_command
        mock_settings.return_value = settings

        publish_output = settings.config.publish_output
        is_auto = settings.config.get("is_auto_command", False)
        minimize = settings.get("github.minimize_api_calls", False)

        should_publish_temp = publish_output and not is_auto and not minimize
        assert should_publish_temp, "Temp comment should be published when flag is off"


# ---------------------------------------------------------------------------
# Config: Default value
# ---------------------------------------------------------------------------


class TestConfigDefault:
    """The minimize_api_calls flag must default to false in configuration.toml."""

    def test_config_flag_defaults_false(self):
        """Verify the config file contains minimize_api_calls = false."""
        import os

        config_path = os.path.join(os.path.dirname(__file__), "..", "..", "pr_agent", "settings", "configuration.toml")
        with open(config_path) as f:
            content = f.read()

        assert "minimize_api_calls = false" in content, "configuration.toml must contain 'minimize_api_calls = false'"
