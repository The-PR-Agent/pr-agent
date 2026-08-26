import unittest
from unittest.mock import MagicMock, patch

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import AzureDevopsProvider


class TestAzureDevopsProviderPublishComment(unittest.TestCase):
    @patch("pr_agent.git_providers.azuredevops_provider.get_settings")
    def test_publish_comment_default_closed(self, mock_get_settings):
        # Simulate config with no default_comment_status
        mock_settings = MagicMock()
        mock_settings.azure_devops.get.return_value = "closed"
        mock_settings.config.publish_output_progress = True
        mock_get_settings.return_value = mock_settings

        with patch.object(AzureDevopsProvider, "_get_azure_devops_client", return_value=(MagicMock(), MagicMock())):
            provider = AzureDevopsProvider()
            provider.workspace_slug = "ws"
            provider.repo_slug = "repo"
            provider.pr_num = 1

            # Patch CommentThread and create_thread
            with patch("pr_agent.git_providers.azuredevops_provider.CommentThread") as MockThread:
                provider.azure_devops_client.create_thread.return_value.comments = [MagicMock()]
                provider.azure_devops_client.create_thread.return_value.comments[0].thread_id = 123
                provider.azure_devops_client.create_thread.return_value.id = 123

                provider.publish_comment("test comment")
                args, kwargs = MockThread.call_args
                assert kwargs.get("status") == "closed"

    @patch("pr_agent.git_providers.azuredevops_provider.get_settings")
    def test_publish_comment_active(self, mock_get_settings):
        # Simulate config with default_comment_status = "active"
        mock_settings = MagicMock()
        mock_settings.azure_devops.get.return_value = "active"
        mock_settings.config.publish_output_progress = True
        mock_get_settings.return_value = mock_settings

        with patch.object(AzureDevopsProvider, "_get_azure_devops_client", return_value=(MagicMock(), MagicMock())):
            provider = AzureDevopsProvider()
            provider.workspace_slug = "ws"
            provider.repo_slug = "repo"
            provider.pr_num = 1

            # Patch CommentThread and create_thread
            with patch("pr_agent.git_providers.azuredevops_provider.CommentThread") as MockThread:
                provider.azure_devops_client.create_thread.return_value.comments = [MagicMock()]
                provider.azure_devops_client.create_thread.return_value.comments[0].thread_id = 123
                provider.azure_devops_client.create_thread.return_value.id = 123

                provider.publish_comment("test comment")
                args, kwargs = MockThread.call_args
                assert kwargs.get("status") == "active"

    def test_default_comment_status_from_config_file(self):
        # Import get_settings directly to read from configuration.toml
        status = get_settings().azure_devops.default_comment_status
        # The expected value should match what's in your configuration.toml
        self.assertEqual(status, "closed")


class TestAzureDevopsProviderCommitUrl(unittest.TestCase):
    def test_get_latest_commit_url_reencodes_spaces(self):
        # workspace/repo slugs are stored decoded for the REST API; the web URL must
        # re-encode them so project/repo names with spaces don't emit raw spaces
        with patch.object(AzureDevopsProvider, "_get_azure_devops_client", return_value=(MagicMock(), MagicMock())):
            provider = AzureDevopsProvider()
            provider.workspace_slug = "Dev Project"
            provider.repo_slug = "repo name"
            provider.pr_num = 1234

            client = provider.azure_devops_client
            client.normalized_url = "https://dev.azure.com/org"
            commit = MagicMock()
            commit.commit_id = "abc123"
            client.get_pull_request_commits.return_value = [commit]

            url = provider.get_latest_commit_url()

            assert url == "https://dev.azure.com/org/Dev%20Project/_git/repo%20name/commit/abc123"
            assert " " not in url