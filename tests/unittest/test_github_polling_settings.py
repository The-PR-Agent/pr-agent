from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.config_loader import get_settings, global_settings
from pr_agent.servers import github_polling


def test_process_comment_sync_scopes_settings_per_comment():
    captured = {}
    publish_progress_before = global_settings.get("CONFIG.PUBLISH_OUTPUT_PROGRESS")
    describe_as_comment_before = global_settings.get("pr_description.publish_description_as_comment")

    # Patch one level below run_handle_request so the real asyncio.run path
    # executes and the test pins that the contextvar survives it.
    async def fake_async_handle_request(pr_url, rest_of_comment, comment_id, git_provider):
        settings = get_settings()
        captured["settings"] = settings
        captured["publish_progress"] = settings.get("CONFIG.PUBLISH_OUTPUT_PROGRESS")
        captured["describe_as_comment"] = settings.get("pr_description.publish_description_as_comment")
        # Simulate what apply_repo_settings does mid-run: mutate the active settings.
        settings.set("config.leaky_test_key", "from-another-pr")
        return True

    with (
        patch.object(github_polling, "get_git_provider", return_value=MagicMock()),
        patch.object(github_polling, "async_handle_request", side_effect=fake_async_handle_request),
        patch.object(github_polling, "litellm_callbacks_registered", return_value=False),
    ):
        github_polling.process_comment_sync("https://example/pr/1", "/review", 123)

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
    tickets_before = global_settings.get("related_tickets", None)
    seen = []

    async def fake_async_handle_request(pr_url, rest_of_comment, comment_id, git_provider):
        settings = get_settings()
        seen.append(settings.get("related_tickets", None))
        settings.set("related_tickets", [{"ticket_id": 1, "pr": pr_url}])
        return True

    with (
        patch.object(github_polling, "get_git_provider", return_value=MagicMock()),
        patch.object(github_polling, "async_handle_request", side_effect=fake_async_handle_request),
        patch.object(github_polling, "litellm_callbacks_registered", return_value=False),
    ):
        github_polling.process_comment_sync("https://example/pr/1", "/review", 1)
        github_polling.process_comment_sync("https://example/pr/2", "/review", 2)

    # Neither comment saw the other's tickets, and the base settings kept their prior value.
    assert seen == [tickets_before, tickets_before]
    assert global_settings.get("related_tickets", None) == tickets_before


@pytest.mark.asyncio
async def test_failed_comment_does_not_pin_settings_on_the_calling_context():
    # request_cycle_context has no try/finally around its yield, so an exception
    # crossing the with-body would skip the ContextVar reset and leave this
    # comment's settings active for everything that runs later in the task.
    settings_before = get_settings()

    with patch.object(github_polling, "get_git_provider", side_effect=ValueError("bad PR url")):
        await github_polling.process_comment("https://example/pr/3", "/review", 789)

    assert get_settings() is settings_before


def test_cancellation_does_not_pin_settings_on_the_calling_context():
    # BaseException is not caught by the handlers' except Exception, so it must
    # still leave the scope through _polling_settings_scope's finally.
    settings_before = get_settings()

    with patch.object(github_polling, "get_git_provider", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            github_polling.process_comment_sync("https://example/pr/4", "/review", 999)

    assert get_settings() is settings_before
