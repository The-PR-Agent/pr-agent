from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.config_loader import get_settings, global_settings
from pr_agent.servers import github_polling


def test_process_comment_sync_scopes_settings_per_comment():
    captured = {}
    publish_progress_before = global_settings.get("CONFIG.PUBLISH_OUTPUT_PROGRESS")
    describe_as_comment_before = global_settings.get("pr_description.publish_description_as_comment")

    def fake_run_handle_request(pr_url, rest_of_comment, comment_id, git_provider):
        settings = get_settings()
        captured["settings"] = settings
        captured["publish_progress"] = settings.get("CONFIG.PUBLISH_OUTPUT_PROGRESS")
        captured["describe_as_comment"] = settings.get("pr_description.publish_description_as_comment")
        # Simulate what apply_repo_settings does mid-run: mutate the active settings.
        settings.set("config.leaky_test_key", "from-another-pr")
        return True

    with (
        patch.object(github_polling, "get_git_provider", return_value=MagicMock()),
        patch.object(github_polling, "run_handle_request", side_effect=fake_run_handle_request) as run_mock,
    ):
        github_polling.process_comment_sync("https://example/pr/1", "/review", 123)

    run_mock.assert_called_once()
    assert captured["settings"] is not global_settings
    assert captured["publish_progress"] is False
    assert captured["describe_as_comment"] is True
    assert global_settings.get("config.leaky_test_key", None) is None
    assert global_settings.get("CONFIG.PUBLISH_OUTPUT_PROGRESS") == publish_progress_before
    assert global_settings.get("pr_description.publish_description_as_comment") == describe_as_comment_before


@pytest.mark.asyncio
async def test_process_comment_scopes_settings_per_comment():
    captured = {}

    async def fake_handle_request(pr_url, request, notify=None):
        settings = get_settings()
        captured["settings"] = settings
        captured["publish_progress"] = settings.get("CONFIG.PUBLISH_OUTPUT_PROGRESS")
        settings.set("config.leaky_test_key_async", "from-another-pr")
        return True

    agent = MagicMock()
    agent.handle_request = AsyncMock(side_effect=fake_handle_request)

    with (
        patch.object(github_polling, "get_git_provider", return_value=MagicMock()),
        patch.object(github_polling, "PRAgent", return_value=agent),
    ):
        await github_polling.process_comment("https://example/pr/2", "/describe", 456)

    agent.handle_request.assert_awaited_once()
    assert captured["settings"] is not global_settings
    assert captured["publish_progress"] is False
    assert global_settings.get("config.leaky_test_key_async", None) is None


def test_consecutive_comments_do_not_share_state():
    seen = []

    def fake_run_handle_request(pr_url, rest_of_comment, comment_id, git_provider):
        settings = get_settings()
        seen.append(settings.get("related_tickets", None))
        settings.set("related_tickets", [{"ticket_id": 1, "pr": pr_url}])
        return True

    with (
        patch.object(github_polling, "get_git_provider", return_value=MagicMock()),
        patch.object(github_polling, "run_handle_request", side_effect=fake_run_handle_request),
    ):
        github_polling.process_comment_sync("https://example/pr/1", "/review", 1)
        github_polling.process_comment_sync("https://example/pr/2", "/review", 2)

    assert seen == [None, None]
    assert global_settings.get("related_tickets", None) is None
