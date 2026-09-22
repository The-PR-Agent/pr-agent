import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from requests.exceptions import HTTPError, Timeout

from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.tools.pr_update_changelog import PRUpdateChangelog


class _CustomProvider:
    """Provider that opts in without inheriting from GithubProvider."""

    def supports_changelog_update_review(self) -> bool:
        return True


class TestPRUpdateChangelog:
    """Test suite for the PR Update Changelog functionality."""

    @pytest.fixture
    def mock_git_provider(self):
        """Create a mock git provider."""
        provider = MagicMock()
        provider.get_pr_branch.return_value = "feature-branch"
        provider.get_pr_file_content.return_value = ""
        provider.pr.title = "Test PR"
        provider.get_pr_description.return_value = "Test description"
        provider.get_commit_messages.return_value = "fix: test commit"
        provider.get_languages.return_value = {"Python": 80, "JavaScript": 20}
        provider.get_files.return_value = ["test.py", "test.js"]
        return provider

    @pytest.fixture
    def mock_ai_handler(self):
        """Create a mock AI handler."""
        handler = MagicMock()
        handler.chat_completion = AsyncMock(return_value=("Test changelog entry", "stop"))
        return handler

    @pytest.fixture
    def changelog_tool(self, mock_git_provider, mock_ai_handler):
        """Create a PRUpdateChangelog instance with mocked dependencies."""
        with patch('pr_agent.tools.pr_update_changelog.get_git_provider', return_value=lambda url: mock_git_provider), \
             patch('pr_agent.tools.pr_update_changelog.get_main_pr_language', return_value="Python"), \
             patch('pr_agent.tools.pr_update_changelog.get_settings') as mock_settings:

            # Configure mock settings
            mock_settings.return_value.pr_update_changelog.push_changelog_changes = False
            mock_settings.return_value.pr_update_changelog.extra_instructions = ""
            mock_settings.return_value.pr_update_changelog_prompt.system = "System prompt"
            mock_settings.return_value.pr_update_changelog_prompt.user = "User prompt"
            mock_settings.return_value.config.temperature = 0.2

            tool = PRUpdateChangelog(
                "https://gitlab.com/test/repo/-/merge_requests/1", ai_handler=lambda: mock_ai_handler
            )
            return tool

    def test_get_changelog_file_with_existing_content(self, changelog_tool, mock_git_provider):
        """Test retrieving existing changelog content."""
        # Arrange
        existing_content = "# Changelog\n\n## v1.0.0\n- Initial release\n- Bug fixes"
        mock_git_provider.get_pr_file_content.return_value = existing_content

        # Act
        changelog_tool._get_changelog_file()

        # Assert
        assert changelog_tool.changelog_file == existing_content
        assert "# Changelog" in changelog_tool.changelog_file_str

    def test_get_changelog_file_with_no_existing_content(self, changelog_tool, mock_git_provider):
        """Test handling when no changelog file exists."""
        # Arrange
        mock_git_provider.get_pr_file_content.return_value = ""

        # Act
        changelog_tool._get_changelog_file()

        # Assert
        assert changelog_tool.changelog_file == ""
        assert "Example:" in changelog_tool.changelog_file_str  # Default template

    def test_get_changelog_file_with_bytes_content(self, changelog_tool, mock_git_provider):
        """Test handling when git provider returns bytes instead of string."""
        # Arrange
        content_bytes = b"# Changelog\n\n## v1.0.0\n- Initial release"
        mock_git_provider.get_pr_file_content.return_value = content_bytes

        # Act
        changelog_tool._get_changelog_file()

        # Assert
        assert isinstance(changelog_tool.changelog_file, str)
        assert changelog_tool.changelog_file == "# Changelog\n\n## v1.0.0\n- Initial release"

    def test_get_changelog_file_with_exception(self, changelog_tool, mock_git_provider):
        """Test handling exceptions during file retrieval."""
        # Arrange
        mock_git_provider.get_pr_file_content.side_effect = Exception("Network error")

        # Act
        changelog_tool._get_changelog_file()

        # Assert
        assert changelog_tool.changelog_file == ""
        assert changelog_tool.changelog_file_str == ""  # Exception should result in empty string, no default template

    def test_prepare_changelog_update_with_existing_content(self, changelog_tool):
        """Test preparing changelog update when existing content exists."""
        # Arrange
        changelog_tool.prediction = "## v1.1.0\n- New feature\n- Bug fix"
        changelog_tool.changelog_file = "# Changelog\n\n## v1.0.0\n- Initial release"
        changelog_tool.commit_changelog = True

        # Act
        new_content, answer = changelog_tool._prepare_changelog_update()

        # Assert
        assert new_content.startswith("## v1.1.0\n- New feature\n- Bug fix\n\n")
        assert "# Changelog\n\n## v1.0.0\n- Initial release" in new_content
        assert answer == "## v1.1.0\n- New feature\n- Bug fix"

    def test_prepare_changelog_update_without_existing_content(self, changelog_tool):
        """Test preparing changelog update when no existing content."""
        # Arrange
        changelog_tool.prediction = "## v1.0.0\n- Initial release"
        changelog_tool.changelog_file = ""
        changelog_tool.commit_changelog = True

        # Act
        new_content, answer = changelog_tool._prepare_changelog_update()

        # Assert
        assert new_content == "## v1.0.0\n- Initial release"
        assert answer == "## v1.0.0\n- Initial release"

    def test_prepare_changelog_update_no_commit(self, changelog_tool):
        """Test preparing changelog update when not committing."""
        # Arrange
        changelog_tool.prediction = "## v1.1.0\n- New feature"
        changelog_tool.changelog_file = ""
        changelog_tool.commit_changelog = False

        # Act
        new_content, answer = changelog_tool._prepare_changelog_update()

        # Assert
        assert new_content == "## v1.1.0\n- New feature"
        assert "to commit the new content" in answer

    def _make_no_push_provider(self, extra_spec=None):
        spec = ["publish_comment", "remove_initial_comment", "get_pr_branch", "get_pr_description",
                "get_commit_messages", "get_languages", "get_files", "get_pr_file_content",
                "is_supported", "supports_changelog_update_review", "pr"]
        if extra_spec:
            spec += extra_spec
        provider = MagicMock(spec=spec)
        provider.pr = MagicMock()
        provider.pr.title = "Test PR"
        provider.get_pr_branch.return_value = "feature-branch"
        provider.get_pr_description.return_value = "Test description"
        provider.get_commit_messages.return_value = "fix: test commit"
        provider.get_languages.return_value = {"Python": 80, "JavaScript": 20}
        provider.get_files.return_value = ["test.py", "test.js"]
        provider.get_pr_file_content.return_value = ""
        provider.supports_changelog_update_review.return_value = False
        return provider

    def _make_push_provider(self):
        provider = self._make_no_push_provider(extra_spec=["create_or_update_pr_file"])
        provider.is_supported.return_value = True
        return provider

    @staticmethod
    def _configure_settings(mock_settings, publish_output=True):
        settings = mock_settings.return_value
        settings.pr_update_changelog.push_changelog_changes = True
        settings.pr_update_changelog.extra_instructions = ""
        settings.pr_update_changelog_prompt.system = ""
        settings.pr_update_changelog_prompt.user = ""
        settings.config.publish_output = publish_output
        settings.config.temperature = 0.2
        settings.get.return_value = {}

    @pytest.mark.asyncio
    async def test_strict_read_error_generates_one_fallback_never_writes_and_reraises_original(
            self, mock_ai_handler):
        provider = self._make_push_provider()
        read_error = RuntimeError("read unavailable")
        provider.get_pr_file_content.side_effect = read_error

        with patch("pr_agent.tools.pr_update_changelog.get_git_provider", return_value=lambda url: provider), \
             patch("pr_agent.tools.pr_update_changelog.get_main_pr_language", return_value="Python"), \
             patch("pr_agent.tools.pr_update_changelog.retry_with_fallback_models") as retry, \
             patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings:
            self._configure_settings(mock_settings)
            tool = PRUpdateChangelog("https://example.com/pr/1", ai_handler=lambda: mock_ai_handler)
            tool.prediction = "## v1.1.0\n- Safe generated entry"

            with pytest.raises(RuntimeError) as exc_info:
                await tool.run()

        assert exc_info.value is read_error
        provider.get_pr_file_content.assert_called_once_with(
            "CHANGELOG.md", "feature-branch", propagate_errors=True
        )
        provider.create_or_update_pr_file.assert_not_called()
        assert retry.await_count == 1
        fallback_calls = [
            call for call in provider.publish_comment.call_args_list
            if "not pushed" in call.args[0]
        ]
        assert len(fallback_calls) == 1
        assert "Safe generated entry" in fallback_calls[0].args[0]
        provider.remove_initial_comment.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_strict_read_error_skips_configuration_rendering(self, mock_ai_handler):
        provider = self._make_push_provider()
        read_error = RuntimeError("read unavailable")
        provider.get_pr_file_content.side_effect = read_error

        with patch("pr_agent.tools.pr_update_changelog.get_git_provider", return_value=lambda url: provider), \
             patch("pr_agent.tools.pr_update_changelog.get_main_pr_language", return_value="Python"), \
             patch("pr_agent.tools.pr_update_changelog.retry_with_fallback_models"), \
             patch("pr_agent.tools.pr_update_changelog.show_relevant_configurations",
                   side_effect=TypeError("invalid skip_keys")) as render_config, \
             patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings:
            self._configure_settings(mock_settings)
            mock_settings.return_value.get.return_value = {"output_relevant_configurations": True}
            tool = PRUpdateChangelog("https://example.com/pr/1", ai_handler=lambda: mock_ai_handler)
            tool.prediction = "## v1.1.0\n- Safe generated entry"

            with pytest.raises(RuntimeError) as exc_info:
                await tool.run()

        assert exc_info.value is read_error
        render_config.assert_not_called()
        provider.create_or_update_pr_file.assert_not_called()
        fallback_calls = [
            call for call in provider.publish_comment.call_args_list
            if "not pushed" in call.args[0]
        ]
        assert len(fallback_calls) == 1
        assert "Safe generated entry" in fallback_calls[0].args[0]
        provider.remove_initial_comment.assert_called_once_with()

    def test_strict_read_setup_failure_attempts_fallback_and_reraises_original(self):
        provider = self._make_push_provider()
        read_error = RuntimeError("read unavailable")
        setup_error = RuntimeError("handler unavailable")
        provider.get_pr_file_content.side_effect = read_error
        handler_factory = MagicMock(side_effect=setup_error)

        with patch("pr_agent.tools.pr_update_changelog.get_git_provider", return_value=lambda url: provider), \
             patch("pr_agent.tools.pr_update_changelog.get_main_pr_language", return_value="Python"), \
             patch("pr_agent.tools.pr_update_changelog.get_logger") as mock_get_logger, \
             patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings:
            self._configure_settings(mock_settings)

            with pytest.raises(RuntimeError) as exc_info:
                PRUpdateChangelog("https://example.com/pr/1", ai_handler=handler_factory)

        assert exc_info.value is read_error
        handler_factory.assert_called_once_with()
        provider.create_or_update_pr_file.assert_not_called()
        fallback_calls = [
            call for call in provider.publish_comment.call_args_list
            if "not pushed" in call.args[0]
        ]
        assert len(fallback_calls) == 1
        assert "could not be generated" in fallback_calls[0].args[0]
        mock_get_logger.return_value.exception.assert_called_once_with(
            "Failed to initialize changelog generation after a read error: handler unavailable"
        )

    @pytest.mark.asyncio
    async def test_strict_read_fallback_failure_does_not_mask_original(self, mock_ai_handler):
        provider = self._make_push_provider()
        read_error = RuntimeError("read unavailable")
        fallback_error = RuntimeError("comment unavailable")
        provider.get_pr_file_content.side_effect = read_error

        def publish_comment(_body, is_temporary=False):
            if not is_temporary:
                raise fallback_error

        provider.publish_comment.side_effect = publish_comment

        with patch("pr_agent.tools.pr_update_changelog.get_git_provider", return_value=lambda url: provider), \
             patch("pr_agent.tools.pr_update_changelog.get_main_pr_language", return_value="Python"), \
             patch("pr_agent.tools.pr_update_changelog.retry_with_fallback_models"), \
             patch("pr_agent.tools.pr_update_changelog.get_logger") as mock_get_logger, \
             patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings:
            self._configure_settings(mock_settings)
            tool = PRUpdateChangelog("https://example.com/pr/1", ai_handler=lambda: mock_ai_handler)
            tool.prediction = "## v1.1.0\n- Safe generated entry"

            with pytest.raises(RuntimeError) as exc_info:
                await tool.run()

        assert exc_info.value is read_error
        provider.create_or_update_pr_file.assert_not_called()
        mock_get_logger.return_value.exception.assert_called_once_with(
            "Failed to publish changelog fallback after a read error: comment unavailable"
        )

    @pytest.mark.asyncio
    async def test_strict_read_progress_failure_still_generates_fallback_and_reraises_original(
            self, mock_ai_handler):
        provider = self._make_push_provider()
        read_error = RuntimeError("read unavailable")
        progress_error = RuntimeError("progress unavailable")
        provider.get_pr_file_content.side_effect = read_error

        def publish_comment(_body, is_temporary=False):
            if is_temporary:
                raise progress_error

        provider.publish_comment.side_effect = publish_comment

        with patch("pr_agent.tools.pr_update_changelog.get_git_provider", return_value=lambda url: provider), \
             patch("pr_agent.tools.pr_update_changelog.get_main_pr_language", return_value="Python"), \
             patch("pr_agent.tools.pr_update_changelog.retry_with_fallback_models") as retry, \
             patch("pr_agent.tools.pr_update_changelog.get_logger") as mock_get_logger, \
             patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings:
            self._configure_settings(mock_settings)
            tool = PRUpdateChangelog("https://example.com/pr/1", ai_handler=lambda: mock_ai_handler)
            tool.prediction = "## v1.1.0\n- Safe generated entry"

            with pytest.raises(RuntimeError) as exc_info:
                await tool.run()

        assert exc_info.value is read_error
        assert retry.await_count == 1
        provider.create_or_update_pr_file.assert_not_called()
        fallback_calls = [
            call for call in provider.publish_comment.call_args_list
            if "not pushed" in call.args[0]
        ]
        assert len(fallback_calls) == 1
        assert "Safe generated entry" in fallback_calls[0].args[0]
        provider.remove_initial_comment.assert_not_called()
        mock_get_logger.return_value.exception.assert_called_once_with(
            "Failed to publish changelog progress after a read error: progress unavailable"
        )

    @pytest.mark.asyncio
    async def test_strict_read_generation_failure_attempts_fallback_and_reraises_original(
            self, mock_ai_handler):
        provider = self._make_push_provider()
        read_error = RuntimeError("read unavailable")
        generation_error = RuntimeError("generation unavailable")
        provider.get_pr_file_content.side_effect = read_error

        with patch("pr_agent.tools.pr_update_changelog.get_git_provider", return_value=lambda url: provider), \
             patch("pr_agent.tools.pr_update_changelog.get_main_pr_language", return_value="Python"), \
             patch("pr_agent.tools.pr_update_changelog.retry_with_fallback_models",
                   side_effect=generation_error) as retry, \
             patch("pr_agent.tools.pr_update_changelog.get_logger") as mock_get_logger, \
             patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings:
            self._configure_settings(mock_settings)
            tool = PRUpdateChangelog("https://example.com/pr/1", ai_handler=lambda: mock_ai_handler)

            with pytest.raises(RuntimeError) as exc_info:
                await tool.run()

        assert exc_info.value is read_error
        assert retry.await_count == 1
        provider.create_or_update_pr_file.assert_not_called()
        fallback_calls = [
            call for call in provider.publish_comment.call_args_list
            if "not pushed" in call.args[0]
        ]
        assert len(fallback_calls) == 1
        assert "could not be generated" in fallback_calls[0].args[0]
        provider.remove_initial_comment.assert_called_once_with()
        mock_get_logger.return_value.exception.assert_called_once_with(
            "Failed to generate changelog fallback after a read error: generation unavailable"
        )

    def test_custom_provider_without_strict_keyword_fails_closed_without_retry(self, mock_ai_handler):
        provider = self._make_push_provider()

        def legacy_getter(_file_path, _branch):
            return "existing content"

        provider.get_pr_file_content.side_effect = legacy_getter

        with patch("pr_agent.tools.pr_update_changelog.get_git_provider", return_value=lambda url: provider), \
             patch("pr_agent.tools.pr_update_changelog.get_main_pr_language", return_value="Python"), \
             patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings:
            self._configure_settings(mock_settings)
            tool = PRUpdateChangelog("https://example.com/pr/1", ai_handler=lambda: mock_ai_handler)

        assert isinstance(tool.changelog_read_error, TypeError)
        assert provider.get_pr_file_content.call_count == 1
        provider.get_pr_file_content.assert_called_once_with(
            "CHANGELOG.md", "feature-branch", propagate_errors=True
        )

    @pytest.mark.parametrize("content", ["", "# Changelog\n\n## v1.0.0\n- Existing entry"])
    def test_strict_read_accepts_successful_empty_and_nonempty_content(self, mock_ai_handler, content):
        provider = self._make_push_provider()
        provider.get_pr_file_content.return_value = content

        with patch("pr_agent.tools.pr_update_changelog.get_git_provider", return_value=lambda url: provider), \
             patch("pr_agent.tools.pr_update_changelog.get_main_pr_language", return_value="Python"), \
             patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings:
            self._configure_settings(mock_settings)
            tool = PRUpdateChangelog("https://example.com/pr/1", ai_handler=lambda: mock_ai_handler)

        assert tool.changelog_read_error is None
        assert tool.changelog_file == content
        provider.get_pr_file_content.assert_called_once_with(
            "CHANGELOG.md", "feature-branch", propagate_errors=True
        )

    @pytest.mark.asyncio
    async def test_output_disabled_keeps_lenient_read_and_never_writes(self, mock_ai_handler):
        provider = self._make_push_provider()
        provider.get_pr_file_content.side_effect = RuntimeError("read unavailable")

        with patch("pr_agent.tools.pr_update_changelog.get_git_provider", return_value=lambda url: provider), \
             patch("pr_agent.tools.pr_update_changelog.get_main_pr_language", return_value="Python"), \
             patch("pr_agent.tools.pr_update_changelog.retry_with_fallback_models") as retry, \
             patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings:
            self._configure_settings(mock_settings, publish_output=False)
            tool = PRUpdateChangelog("https://example.com/pr/1", ai_handler=lambda: mock_ai_handler)
            tool.prediction = "## v1.1.0\n- Safe generated entry"

            await tool.run()

        assert tool.changelog_read_error is None
        assert retry.await_count == 1
        provider.get_pr_file_content.assert_called_once_with("CHANGELOG.md", "feature-branch")
        provider.create_or_update_pr_file.assert_not_called()
        provider.publish_comment.assert_not_called()
        provider.remove_initial_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_without_push_support(self, mock_ai_handler):
        """When the provider can't push (no create_or_update_pr_file), the changelog must still
        be generated and published as a comment (graceful degradation), not dropped entirely."""
        provider = self._make_no_push_provider()  # spec omits create_or_update_pr_file
        provider.is_supported.return_value = True

        with patch('pr_agent.tools.pr_update_changelog.get_git_provider', return_value=lambda url: provider), \
             patch('pr_agent.tools.pr_update_changelog.get_main_pr_language', return_value="Python"), \
             patch('pr_agent.tools.pr_update_changelog.retry_with_fallback_models'), \
             patch('pr_agent.tools.pr_update_changelog.get_settings') as mock_settings:
            mock_settings.return_value.pr_update_changelog.push_changelog_changes = True
            mock_settings.return_value.config.publish_output = True
            mock_settings.return_value.pr_update_changelog.extra_instructions = ""
            mock_settings.return_value.pr_update_changelog_prompt.system = ""
            mock_settings.return_value.pr_update_changelog_prompt.user = ""
            mock_settings.return_value.get.return_value = {}
            tool = PRUpdateChangelog("https://example.com/pr/123", ai_handler=lambda: mock_ai_handler)

            # Push isn't possible -> degrade to comment mode (don't push, don't drop the output).
            assert tool.push_skipped_reason == "not supported for this git provider"
            assert tool.commit_changelog is False

            tool.prediction = "## v1.1.0\n- New feature"
            await tool.run()

            published = " ".join(str(c) for c in provider.publish_comment.call_args_list)
            assert "Changelog updates" in published  # the generated changelog was posted
            assert "not pushed" in published          # with a note it wasn't committed

    @pytest.mark.asyncio
    async def test_run_restricted_mode_publishes_comment_instead_of_pushing(self, mock_ai_handler):
        """restricted_mode: the provider supports the push API, but is_supported('push_code') is
        False, so the changelog must be published as a comment rather than pushed to the repo."""
        provider = self._make_no_push_provider(extra_spec=["create_or_update_pr_file"])
        provider.is_supported.return_value = False  # restricted_mode disables push_code

        with patch('pr_agent.tools.pr_update_changelog.get_git_provider', return_value=lambda url: provider), \
             patch('pr_agent.tools.pr_update_changelog.get_main_pr_language', return_value="Python"), \
             patch('pr_agent.tools.pr_update_changelog.retry_with_fallback_models'), \
             patch('pr_agent.tools.pr_update_changelog.get_settings') as mock_settings:
            mock_settings.return_value.pr_update_changelog.push_changelog_changes = True
            mock_settings.return_value.config.publish_output = True
            mock_settings.return_value.pr_update_changelog.extra_instructions = ""
            mock_settings.return_value.pr_update_changelog_prompt.system = ""
            mock_settings.return_value.pr_update_changelog_prompt.user = ""
            mock_settings.return_value.get.return_value = {}
            tool = PRUpdateChangelog("https://example.com/pr/1", ai_handler=lambda: mock_ai_handler)

            assert tool.push_skipped_reason == "restricted by configuration (restricted_mode)"
            assert tool.commit_changelog is False
            provider.is_supported.assert_called_with("push_code")

            tool.prediction = "## v1.1.0\n- feat"
            await tool.run()

            provider.create_or_update_pr_file.assert_not_called()  # never pushed
            published = " ".join(str(c) for c in provider.publish_comment.call_args_list)
            assert "Changelog updates" in published
            assert "not pushed" in published

    @pytest.mark.asyncio
    async def test_run_with_push_support(self, changelog_tool, mock_git_provider):
        """Test running changelog update when git provider supports pushing."""
        # Arrange
        mock_git_provider.create_or_update_pr_file = MagicMock()
        changelog_tool.commit_changelog = True
        changelog_tool.prediction = "## v1.1.0\n- New feature"

        with patch('pr_agent.tools.pr_update_changelog.get_settings') as mock_settings, \
             patch('pr_agent.tools.pr_update_changelog.retry_with_fallback_models') as mock_retry, \
             patch('pr_agent.tools.pr_update_changelog.asyncio.sleep', new_callable=AsyncMock) as sleep:

            mock_settings.return_value.pr_update_changelog.push_changelog_changes = True
            mock_settings.return_value.pr_update_changelog.get.return_value = True
            mock_settings.return_value.config.publish_output = True
            mock_settings.return_value.config.git_provider = "gitlab"
            mock_retry.return_value = None

            # Act
            await changelog_tool.run()

            # Assert
            mock_git_provider.create_or_update_pr_file.assert_called_once()
            call_args = mock_git_provider.create_or_update_pr_file.call_args
            assert call_args[1]['file_path'] == 'CHANGELOG.md'
            assert call_args[1]['branch'] == 'feature-branch'
            sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_push_changelog_update_creates_review_when_supported(self, changelog_tool, mock_git_provider):
        """When supported, pushing the changelog creates a PR review on the committed changes."""
        mock_git_provider.create_or_update_pr_file = MagicMock(return_value="commit-123")
        mock_git_provider.get_pr_branch.return_value = "feature-branch"
        mock_git_provider.supports_changelog_update_review.return_value = True
        mock_git_provider.pr.get_commits.return_value = ["commit-123"]
        mock_git_provider.pr.create_review = MagicMock()
        new_content = "# Updated changelog content"
        answer = "Line 1\nLine 2"

        with patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings, patch(
            "pr_agent.tools.pr_update_changelog.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            mock_settings.return_value.pr_update_changelog.get.return_value = True

            await changelog_tool._push_changelog_update(new_content, answer)

            mock_git_provider.create_or_update_pr_file.assert_called_once_with(
                file_path="CHANGELOG.md",
                branch="feature-branch",
                contents=new_content,
                message="[skip ci] Update CHANGELOG.md",
            )
            mock_git_provider.pr.create_review.assert_called_once_with(
                commit="commit-123",
                event="COMMENT",
                comments=[
                    dict(
                        body="CHANGELOG.md update",
                        path="CHANGELOG.md",
                        line=2,
                        start_line=1,
                    )
                ],
            )
            mock_git_provider.publish_comment.assert_not_called()
            mock_git_provider.pr.get_commits.assert_not_called()
            sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_push_changelog_update_yields_during_wait_before_review(
        self, changelog_tool, mock_git_provider
    ):
        events = []
        peer_progressed = asyncio.Event()
        mock_git_provider.create_or_update_pr_file.side_effect = (
            lambda **_kwargs: events.append("write") or "commit-123"
        )
        mock_git_provider.supports_changelog_update_review.return_value = True
        mock_git_provider.pr.create_review.side_effect = lambda **_kwargs: events.append("review")

        async def peer_work():
            events.append("peer progress")
            peer_progressed.set()

        async def cooperative_wait(delay):
            assert delay == 5
            events.append("wait")
            await peer_progressed.wait()

        peer_task = asyncio.create_task(peer_work())
        with patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings, patch(
            "pr_agent.tools.pr_update_changelog.asyncio.sleep", side_effect=cooperative_wait
        ) as sleep:
            mock_settings.return_value.pr_update_changelog.get.return_value = True

            await changelog_tool._push_changelog_update("new content", "answer")

        assert await peer_task is None
        sleep.assert_awaited_once_with(5)
        mock_git_provider.pr.get_commits.assert_not_called()
        assert events == ["write", "wait", "peer progress", "review"]

    @pytest.mark.asyncio
    async def test_push_changelog_update_reviews_exact_written_commit_when_branch_advances(
        self, changelog_tool, mock_git_provider
    ):
        written_commit = object()
        branch_advanced = asyncio.Event()
        mock_git_provider.create_or_update_pr_file.return_value = written_commit
        mock_git_provider.supports_changelog_update_review.return_value = True
        mock_git_provider.pr.get_commits.return_value = [object()]

        async def cooperative_wait(delay):
            assert delay == 5
            branch_advanced.set()

        with patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings, patch(
            "pr_agent.tools.pr_update_changelog.asyncio.sleep", side_effect=cooperative_wait
        ):
            mock_settings.return_value.pr_update_changelog.get.return_value = True

            await changelog_tool._push_changelog_update("new content", "answer")

        assert branch_advanced.is_set()
        mock_git_provider.pr.get_commits.assert_not_called()
        assert mock_git_provider.pr.create_review.call_args.kwargs["commit"] is written_commit

    @pytest.mark.asyncio
    async def test_push_changelog_update_cancellation_after_write_publishes_fallback_and_propagates(
        self, changelog_tool, mock_git_provider
    ):
        entered_wait = asyncio.Event()
        mock_git_provider.create_or_update_pr_file.return_value = object()
        mock_git_provider.supports_changelog_update_review.return_value = True

        async def pending_wait(delay):
            assert delay == 5
            entered_wait.set()
            await asyncio.Event().wait()

        with patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings, patch(
            "pr_agent.tools.pr_update_changelog.asyncio.sleep", side_effect=pending_wait
        ):
            mock_settings.return_value.pr_update_changelog.get.return_value = True
            update_task = asyncio.create_task(
                changelog_tool._push_changelog_update("new content", "answer")
            )
            await entered_wait.wait()
            update_task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await update_task

        mock_git_provider.publish_comment.assert_called_once_with(
            "**Changelog updates: 🔄**\n\nanswer"
        )
        mock_git_provider.pr.get_commits.assert_not_called()
        mock_git_provider.pr.create_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_cancellation_after_write_publishes_fallback_before_cleanup(
        self, changelog_tool, mock_git_provider
    ):
        entered_wait = asyncio.Event()
        mock_git_provider.create_or_update_pr_file.return_value = object()
        mock_git_provider.supports_changelog_update_review.return_value = True
        changelog_tool.commit_changelog = True
        changelog_tool.prediction = "## v1.1.0\n- New feature"

        async def pending_wait(_delay):
            entered_wait.set()
            await asyncio.Event().wait()

        with (
            patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings,
            patch("pr_agent.tools.pr_update_changelog.retry_with_fallback_models"),
            patch("pr_agent.tools.pr_update_changelog.asyncio.sleep", side_effect=pending_wait),
        ):
            mock_settings.return_value.config.publish_output = True
            mock_settings.return_value.pr_update_changelog.get.return_value = True
            mock_settings.return_value.get.return_value = {}
            run_task = asyncio.create_task(changelog_tool.run())
            await entered_wait.wait()
            run_task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await run_task

        assert mock_git_provider.publish_comment.call_args_list == [
            call("Preparing changelog updates...", is_temporary=True),
            call("**Changelog updates: 🔄**\n\n## v1.1.0\n- New feature"),
        ]
        mock_git_provider.pr.create_review.assert_not_called()
        mock_git_provider.remove_initial_comment.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_push_changelog_update_cancellation_fallback_failure_does_not_mask_cancellation(
        self, changelog_tool, mock_git_provider
    ):
        entered_wait = asyncio.Event()
        feedback_error = RuntimeError("comment unavailable")
        mock_git_provider.create_or_update_pr_file.return_value = object()
        mock_git_provider.supports_changelog_update_review.return_value = True
        mock_git_provider.publish_comment.side_effect = feedback_error

        async def pending_wait(_delay):
            entered_wait.set()
            await asyncio.Event().wait()

        with patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings, patch(
            "pr_agent.tools.pr_update_changelog.asyncio.sleep", side_effect=pending_wait
        ), patch("pr_agent.tools.pr_update_changelog.get_logger") as mock_get_logger:
            mock_settings.return_value.pr_update_changelog.get.return_value = True
            update_task = asyncio.create_task(
                changelog_tool._push_changelog_update("new content", "answer")
            )
            await entered_wait.wait()
            update_task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await update_task

        mock_get_logger.return_value.exception.assert_called_once_with(
            "Failed to publish changelog fallback during cancellation: comment unavailable"
        )
        mock_git_provider.pr.create_review.assert_not_called()

    @pytest.mark.parametrize("error_type", [HTTPError, Timeout])
    @pytest.mark.asyncio
    async def test_push_changelog_update_retains_output_and_stops_success_follow_up_after_write_failure(
        self, changelog_tool, mock_git_provider, error_type
    ):
        write_error = error_type("write failed")
        mock_git_provider.create_or_update_pr_file.side_effect = write_error
        mock_git_provider.get_pr_branch.return_value = "feature-branch"
        mock_git_provider.supports_changelog_update_review.return_value = True

        with patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings, patch(
            "pr_agent.tools.pr_update_changelog.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            mock_settings.return_value.pr_update_changelog.get.return_value = True

            with pytest.raises(error_type) as raised:
                await changelog_tool._push_changelog_update("new content", "answer")

        assert raised.value is write_error
        mock_git_provider.create_or_update_pr_file.assert_called_once()
        sleep.assert_not_awaited()
        mock_git_provider.pr.get_commits.assert_not_called()
        mock_git_provider.pr.create_review.assert_not_called()
        mock_git_provider.publish_comment.assert_called_once()
        fallback = mock_git_provider.publish_comment.call_args.args[0]
        assert "answer" in fallback
        assert "could not be confirmed" in fallback
        assert "not pushed" not in fallback

    @pytest.mark.asyncio
    async def test_push_changelog_update_fallback_failure_does_not_mask_write_error(
        self, changelog_tool, mock_git_provider
    ):
        write_error = Timeout("write outcome unknown")
        fallback_error = RuntimeError("comment unavailable")
        mock_git_provider.create_or_update_pr_file.side_effect = write_error
        mock_git_provider.publish_comment.side_effect = fallback_error

        with (
            patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings,
            patch("pr_agent.tools.pr_update_changelog.get_logger") as mock_get_logger,
            pytest.raises(Timeout) as raised,
        ):
            mock_settings.return_value.pr_update_changelog.get.return_value = True
            await changelog_tool._push_changelog_update("new content", "answer")

        assert raised.value is write_error
        mock_git_provider.publish_comment.assert_called_once()
        mock_get_logger.return_value.exception.assert_called_once_with(
            "Failed to publish changelog fallback after a write error: comment unavailable"
        )

    @pytest.mark.asyncio
    async def test_run_preserves_write_failure_after_temporary_comment_cleanup(
        self, changelog_tool, mock_git_provider
    ):
        write_error = HTTPError("403 Client Error")
        mock_git_provider.create_or_update_pr_file.side_effect = write_error
        mock_git_provider.remove_initial_comment.side_effect = RuntimeError("cleanup failed")
        mock_git_provider.supports_changelog_update_review.return_value = True
        changelog_tool.commit_changelog = True
        changelog_tool.prediction = "## v1.1.0\n- New feature"

        with (
            patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings,
            patch("pr_agent.tools.pr_update_changelog.retry_with_fallback_models"),
            patch("pr_agent.tools.pr_update_changelog.asyncio.sleep", new_callable=AsyncMock) as sleep,
        ):
            mock_settings.return_value.config.publish_output = True
            mock_settings.return_value.pr_update_changelog.get.return_value = True
            mock_settings.return_value.get.return_value = {}

            with pytest.raises(HTTPError) as raised:
                await changelog_tool.run()

        assert raised.value is write_error
        assert mock_git_provider.publish_comment.call_args_list == [
            call("Preparing changelog updates...", is_temporary=True),
            call(
                "**Changelog updates:** 🔄\n\n## v1.1.0\n- New feature"
                "\n\n> ⚠️ The repository update could not be confirmed. "
                "The generated changelog is preserved here for recovery."
            ),
        ]
        mock_git_provider.remove_initial_comment.assert_called_once_with()
        sleep.assert_not_awaited()
        mock_git_provider.pr.get_commits.assert_not_called()
        mock_git_provider.pr.create_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_push_changelog_update_skips_review_when_not_supported(self, changelog_tool, mock_git_provider):
        """A provider without the capability is never asked for a commit-scoped review."""
        mock_git_provider.create_or_update_pr_file = MagicMock()
        mock_git_provider.get_pr_branch.return_value = "feature-branch"
        mock_git_provider.supports_changelog_update_review.return_value = False
        new_content = "# Updated changelog content"
        answer = "Changes made"

        with patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings, patch(
            "pr_agent.tools.pr_update_changelog.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            mock_settings.return_value.pr_update_changelog.get.return_value = True

            await changelog_tool._push_changelog_update(new_content, answer)

            mock_git_provider.create_or_update_pr_file.assert_called_once_with(
                file_path="CHANGELOG.md",
                branch="feature-branch",
                contents=new_content,
                message="[skip ci] Update CHANGELOG.md",
            )
            mock_git_provider.pr.get_commits.assert_not_called()
            mock_git_provider.publish_comment.assert_not_called()
            sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_push_changelog_update_falls_back_to_comment_when_written_commit_is_unavailable(
        self, changelog_tool, mock_git_provider
    ):
        """A review-capable provider must not guess the commit when the write returns none."""
        mock_git_provider.create_or_update_pr_file = MagicMock(return_value=None)
        mock_git_provider.get_pr_branch.return_value = "feature-branch"
        mock_git_provider.supports_changelog_update_review.return_value = True
        new_content = "# Updated changelog content"
        answer = "Changes made"

        with patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings, patch(
            "pr_agent.tools.pr_update_changelog.asyncio.sleep", new_callable=AsyncMock
        ):
            mock_settings.return_value.pr_update_changelog.get.return_value = True

            await changelog_tool._push_changelog_update(new_content, answer)

            mock_git_provider.pr.get_commits.assert_not_called()
            mock_git_provider.publish_comment.assert_called_once_with(f"**Changelog updates: 🔄**\n\n{answer}")

    @pytest.mark.asyncio
    async def test_push_changelog_update_falls_back_to_comment_on_review_exception(
        self, changelog_tool, mock_git_provider
    ):
        """When creating a review raises an exception, it falls back to publishing a comment."""
        mock_git_provider.create_or_update_pr_file = MagicMock(return_value="commit-123")
        mock_git_provider.get_pr_branch.return_value = "feature-branch"
        mock_git_provider.supports_changelog_update_review.return_value = True
        mock_git_provider.pr.create_review.side_effect = Exception("API error")
        answer = "Changes made"

        with patch("pr_agent.tools.pr_update_changelog.get_settings") as mock_settings, patch(
            "pr_agent.tools.pr_update_changelog.asyncio.sleep", new_callable=AsyncMock
        ):
            mock_settings.return_value.pr_update_changelog.get.return_value = True

            await changelog_tool._push_changelog_update("new content", answer)

        mock_git_provider.pr.get_commits.assert_not_called()
        mock_git_provider.publish_comment.assert_called_once_with(f"**Changelog updates: 🔄**\n\n{answer}")

    @pytest.mark.asyncio
    async def test_push_changelog_update(self, changelog_tool, mock_git_provider):
        """Test the push changelog update functionality."""
        # Arrange
        mock_git_provider.create_or_update_pr_file = MagicMock()
        mock_git_provider.get_pr_branch.return_value = "feature-branch"
        new_content = "# Updated changelog content"
        answer = "Changes made"

        with patch('pr_agent.tools.pr_update_changelog.get_settings') as mock_settings, \
             patch('pr_agent.tools.pr_update_changelog.asyncio.sleep', new_callable=AsyncMock):

            mock_settings.return_value.pr_update_changelog.get.return_value = True

            # Act
            await changelog_tool._push_changelog_update(new_content, answer)

            # Assert
            mock_git_provider.create_or_update_pr_file.assert_called_once_with(
                file_path="CHANGELOG.md",
                branch="feature-branch",
                contents=new_content,
                message="[skip ci] Update CHANGELOG.md"
            )

    @pytest.mark.asyncio
    async def test_push_changelog_update_never_calls_create_or_update_pr_file_when_push_code_is_unsupported(
            self, changelog_tool, mock_git_provider):
        """A provider that declines `push_code` (e.g. restricted_mode) must never reach
        `create_or_update_pr_file`, even if a future caller invokes this method directly
        without going through `run()`'s own `self.commit_changelog` gate."""
        mock_git_provider.create_or_update_pr_file = MagicMock()
        mock_git_provider.is_supported.return_value = False
        new_content = "# Updated changelog content"
        answer = "Changes made"

        await changelog_tool._push_changelog_update(new_content, answer)

        mock_git_provider.create_or_update_pr_file.assert_not_called()

    def test_gitlab_provider_method_detection(self, changelog_tool, mock_git_provider):
        """Test that the tool correctly detects GitLab provider method availability."""
        # Arrange
        mock_git_provider.create_or_update_pr_file = MagicMock()

        # Act & Assert
        assert hasattr(mock_git_provider, "create_or_update_pr_file")

    @pytest.mark.parametrize(
        "provider_class, expected",
        [(GithubProvider, True), (_CustomProvider, True), (MagicMock, False)],
    )
    def test_supports_changelog_update_review_follows_provider_capability(self, provider_class, expected):
        if provider_class is MagicMock:
            provider = MagicMock()
            provider.supports_changelog_update_review.return_value = False
        else:
            provider = provider_class.__new__(provider_class)
        assert provider.supports_changelog_update_review() is expected

    @pytest.mark.parametrize("existing_content,new_entry,expected_order", [
        (
            "# Changelog\n\n## v1.0.0\n- Old feature",
            "## v1.1.0\n- New feature",
            ["v1.1.0", "v1.0.0"]
        ),
        (
            "",
            "## v1.0.0\n- Initial release",
            ["v1.0.0"]
        ),
        (
            "Some existing content",
            "## v1.0.0\n- New entry",
            ["v1.0.0", "Some existing content"]
        ),
    ])
    def test_changelog_order_preservation(self, changelog_tool, existing_content, new_entry, expected_order):
        """Test that changelog entries are properly ordered (newest first)."""
        # Arrange
        changelog_tool.prediction = new_entry
        changelog_tool.changelog_file = existing_content
        changelog_tool.commit_changelog = True

        # Act
        new_content, _ = changelog_tool._prepare_changelog_update()

        # Assert
        for i, expected in enumerate(expected_order[:-1]):
            current_pos = new_content.find(expected)
            next_pos = new_content.find(expected_order[i + 1])
            assert current_pos < next_pos, f"Expected {expected} to come before {expected_order[i + 1]}"
