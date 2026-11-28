from unittest.mock import MagicMock, patch

from pr_agent.git_providers.azuredevops_provider import (
    MAX_PR_COMMENT_AZURE_LENGTH,
    AzureDevopsProvider,
)


def _provider():
    with patch.object(AzureDevopsProvider, "_get_azure_devops_client", return_value=(MagicMock(), MagicMock())):
        provider = AzureDevopsProvider()
    provider.workspace_slug = "ws"
    provider.repo_slug = "repo"
    provider.pr_num = 1
    provider.azure_devops_client = MagicMock()
    created = MagicMock()
    provider.azure_devops_client.create_thread.return_value.comments = [created]
    provider.azure_devops_client.create_thread.return_value.id = 123
    return provider


def test_publish_comment_caps_a_body_over_the_api_limit():
    provider = _provider()
    body = "C" * (MAX_PR_COMMENT_AZURE_LENGTH + 200)

    with (
        patch("pr_agent.git_providers.azuredevops_provider.Comment") as MockComment,
        patch("pr_agent.git_providers.azuredevops_provider.CommentThread"),
        patch("pr_agent.git_providers.azuredevops_provider.get_settings") as mock_settings,
    ):
        mock_settings.return_value.config.publish_output_progress = True
        mock_settings.return_value.azure_devops.get.return_value = "closed"
        provider.publish_comment(body)

    published = MockComment.call_args.kwargs["content"]
    assert len(published) == MAX_PR_COMMENT_AZURE_LENGTH
    assert published.endswith("...")
    assert published.startswith("C")


def test_publish_comment_keeps_a_body_within_the_limit():
    provider = _provider()
    body = "short review comment"

    with (
        patch("pr_agent.git_providers.azuredevops_provider.Comment") as MockComment,
        patch("pr_agent.git_providers.azuredevops_provider.CommentThread"),
        patch("pr_agent.git_providers.azuredevops_provider.get_settings") as mock_settings,
    ):
        mock_settings.return_value.config.publish_output_progress = True
        mock_settings.return_value.azure_devops.get.return_value = "closed"
        provider.publish_comment(body)

    assert MockComment.call_args.kwargs["content"] == body


def test_edit_comment_caps_a_body_over_the_api_limit():
    provider = _provider()
    comment = MagicMock()
    comment.thread_id = 9
    comment.id = 4
    body = "E" * (MAX_PR_COMMENT_AZURE_LENGTH + 50)

    with patch("pr_agent.git_providers.azuredevops_provider.Comment") as MockComment:
        assert provider.edit_comment(comment, body) is True

    published = MockComment.call_args.kwargs["content"]
    assert len(published) == MAX_PR_COMMENT_AZURE_LENGTH
    assert published.endswith("...")
    assert published.startswith("E")
    provider.azure_devops_client.update_comment.assert_called_once()
