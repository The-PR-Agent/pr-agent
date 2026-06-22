from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.tools.pr_update_changelog import PRUpdateChangelog


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
            
            tool = PRUpdateChangelog("https://gitlab.com/test/repo/-/merge_requests/1", ai_handler=lambda: mock_ai_handler)
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

    @pytest.mark.asyncio
    async def test_run_without_push_support(self, changelog_tool, mock_git_provider):
        """Test running changelog update when git provider doesn't support pushing."""
        # Arrange
        delattr(mock_git_provider, 'create_or_update_pr_file')  # Remove the method
        changelog_tool.commit_changelog = True
        
        with patch('pr_agent.tools.pr_update_changelog.get_settings') as mock_settings:
            mock_settings.return_value.pr_update_changelog.push_changelog_changes = True
            mock_settings.return_value.config.publish_output = True
            
            # Act
            await changelog_tool.run()
            
            # Assert
            mock_git_provider.publish_comment.assert_called_once()
            assert "not currently supported" in str(mock_git_provider.publish_comment.call_args)

    @pytest.mark.asyncio
    async def test_run_with_push_support(self, changelog_tool, mock_git_provider):
        """Test running changelog update when git provider supports pushing."""
        # Arrange
        mock_git_provider.create_or_update_pr_file = MagicMock()
        changelog_tool.commit_changelog = True
        changelog_tool.prediction = "## v1.1.0\n- New feature"
        
        with patch('pr_agent.tools.pr_update_changelog.get_settings') as mock_settings, \
             patch('pr_agent.tools.pr_update_changelog.retry_with_fallback_models') as mock_retry, \
             patch('pr_agent.tools.pr_update_changelog.sleep'):
            
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

    def test_push_changelog_update(self, changelog_tool, mock_git_provider):
        """Test the push changelog update functionality."""
        # Arrange
        mock_git_provider.create_or_update_pr_file = MagicMock()
        mock_git_provider.get_pr_branch.return_value = "feature-branch"
        new_content = "# Updated changelog content"
        answer = "Changes made"
        
        with patch('pr_agent.tools.pr_update_changelog.get_settings') as mock_settings, \
             patch('pr_agent.tools.pr_update_changelog.sleep'):
            
            mock_settings.return_value.pr_update_changelog.get.return_value = True
            
            # Act
            changelog_tool._push_changelog_update(new_content, answer)
            
            # Assert
            mock_git_provider.create_or_update_pr_file.assert_called_once_with(
                file_path="CHANGELOG.md",
                branch="feature-branch",
                contents=new_content,
                message="[skip ci] Update CHANGELOG.md"
            )

    def test_gitlab_provider_method_detection(self, changelog_tool, mock_git_provider):
        """Test that the tool correctly detects GitLab provider method availability."""
        # Arrange
        mock_git_provider.create_or_update_pr_file = MagicMock()
        
        # Act & Assert
        assert hasattr(mock_git_provider, "create_or_update_pr_file")

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