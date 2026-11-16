"""Run-details coverage for /update_changelog comments."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from pr_agent.commands.pr_update_changelog import PRUpdateChangelog
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import FileContentSnapshot
from tests.unittest._run_details_command_helpers import (
    BACKUP,
    PRIMARY,
    TITLE,
    _model_stub,
    _provider,
    _published_bodies,
    _seed_stale_fallback_run,
    command_run_settings,
)


@pytest.fixture(autouse=True)
def _run_settings():
    with command_run_settings() as settings:
        yield settings


# ---------------------------------------------------------------- /update_changelog

EXISTING_CHANGELOG = "# Changelog\n\n## 1.0.0\n- Initial release"
CHANGELOG_ANSWER = "## 1.1.0\n- Added run details\n- Fixed a bug"


def _make_changelog(*, commit, fail_primary=False, provider=None, read_error=None):
    command = PRUpdateChangelog.__new__(PRUpdateChangelog)
    command.changelog_path = "CHANGELOG.md"
    command.git_provider = provider or _provider()
    command.commit_changelog = commit
    command.push_changelog_changes = commit
    command.push_skipped_reason = None
    command.changelog_read_error = read_error
    command.changelog_file = EXISTING_CHANGELOG
    command.changelog_snapshot = FileContentSnapshot(EXISTING_CHANGELOG, True, "captured-revision")
    command.prediction = None
    stub = _model_stub(CHANGELOG_ANSWER, fail_primary)

    async def prepare(model):
        command.prediction = await stub(model)

    command._prepare_prediction = prepare
    return command


def _push_provider():
    provider = _provider()
    provider.is_supported.side_effect = lambda capability: capability in ("gfm_markdown", "push_code")
    provider.get_pr_branch.return_value = "feature-branch"
    provider.create_or_update_pr_file.return_value = "written-commit"
    provider.supports_changelog_update_review.return_value = True
    provider.supports_comment_publish_confirmation.return_value = False
    return provider


async def test_changelog_comment_appends_run_details_once():
    command = _make_changelog(commit=False)

    await command.run()

    [body] = _published_bodies(command.git_provider)
    assert body.startswith(f"**Changelog updates:** 🔄\n\n{CHANGELOG_ANSWER}")
    assert body.count(TITLE) == 1
    assert f"Model: {PRIMARY}" in body


async def test_changelog_comment_omits_run_details_when_disabled():
    get_settings().set("config.output_run_details", False)
    command = _make_changelog(commit=False)

    await command.run()

    [body] = _published_bodies(command.git_provider)
    assert TITLE not in body


async def test_changelog_push_keeps_committed_file_and_review_range_clean():
    provider = _push_provider()
    command = _make_changelog(commit=True, provider=provider)

    with patch("pr_agent.commands.pr_update_changelog.asyncio.sleep", new_callable=AsyncMock):
        await command.run()

    assert provider.create_or_update_pr_file.call_args.kwargs["contents"] == (
        f"{CHANGELOG_ANSWER}\n\n{EXISTING_CHANGELOG}"
    )
    [review_comment] = provider.pr.create_review.call_args.kwargs["comments"]
    assert review_comment["body"] == "CHANGELOG.md update"
    assert review_comment["line"] == len(CHANGELOG_ANSWER.splitlines())
    assert _published_bodies(provider) == []


async def test_changelog_review_fallback_comment_reports_the_fallback_model():
    provider = _push_provider()
    provider.pr.create_review.side_effect = RuntimeError("review rejected")
    command = _make_changelog(commit=True, fail_primary=True, provider=provider)

    with patch("pr_agent.commands.pr_update_changelog.asyncio.sleep", new_callable=AsyncMock):
        await command.run()

    assert TITLE not in provider.create_or_update_pr_file.call_args.kwargs["contents"]
    [body] = _published_bodies(provider)
    assert body.startswith(f"**Changelog updates: 🔄**\n\n{CHANGELOG_ANSWER}")
    assert body.count(TITLE) == 1
    assert f"Model: {BACKUP} (fallback)" in body


async def test_changelog_write_error_fallback_appends_run_details():
    provider = _push_provider()
    provider.create_or_update_pr_file.side_effect = RuntimeError("write failed")
    command = _make_changelog(commit=True, provider=provider)

    with pytest.raises(RuntimeError, match="write failed"):
        await command.run()

    [body] = _published_bodies(provider)
    assert "The repository update could not be confirmed" in body
    assert body.count(TITLE) == 1


async def test_changelog_cancellation_fallback_appends_run_details():
    provider = _push_provider()
    command = _make_changelog(commit=True, provider=provider)

    with (
        patch("pr_agent.commands.pr_update_changelog.asyncio.sleep",
              new_callable=AsyncMock, side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        await command.run()

    [body] = _published_bodies(provider)
    assert body.startswith(f"**Changelog updates: 🔄**\n\n{CHANGELOG_ANSWER}")
    assert body.count(TITLE) == 1


async def test_changelog_read_error_fallback_appends_run_details():
    command = _make_changelog(commit=False, read_error=OSError("changelog unreadable"))

    with pytest.raises(OSError, match="changelog unreadable"):
        await command.run()

    [body] = _published_bodies(command.git_provider)
    assert "could not be read safely" in body
    assert body.count(TITLE) == 1


async def test_changelog_read_error_without_an_answer_has_no_run_details():
    _seed_stale_fallback_run()
    command = _make_changelog(commit=False, read_error=OSError("changelog unreadable"))

    async def unavailable(model):
        raise TimeoutError(f"{model} unavailable")

    command._prepare_prediction = unavailable

    with pytest.raises(OSError, match="changelog unreadable"):
        await command.run()

    [body] = _published_bodies(command.git_provider)
    assert body.startswith("**Changelog update could not be generated.**")
    assert TITLE not in body


def test_changelog_setup_read_error_fallback_never_reports_a_stale_model():
    # __init__ publishes this fallback before run() installs a fresh collector.
    _seed_stale_fallback_run()
    command = _make_changelog(commit=False, read_error=OSError("changelog unreadable"))

    command._publish_changelog_read_error_fallback()

    [body] = _published_bodies(command.git_provider)
    assert body.startswith("**Changelog update could not be generated.**")
    assert TITLE not in body


async def test_changelog_starts_a_fresh_run_context():
    _seed_stale_fallback_run()
    command = _make_changelog(commit=False)

    await command.run()

    [body] = _published_bodies(command.git_provider)
    assert f"Model: {PRIMARY}" in body
    assert "(fallback)" not in body
    assert "stale-model" not in body
