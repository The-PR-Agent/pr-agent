"""Run-details coverage for /ask (/ask_question) and /ask_line."""

from unittest.mock import MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_line_questions import PR_LineQuestions
from pr_agent.tools.pr_questions import PRQuestions
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


# ---------------------------------------------------------------- /ask, /ask_question

def _make_ask(prediction="The change is safe.", fail_primary=False, provider=None):
    get_settings().set("pr_questions.enable_help_text", False)
    tool = PRQuestions.__new__(PRQuestions)
    tool.pr_url = "https://example.com/org/repo/pull/1"
    tool.question_str = "Is this change safe?"
    tool.vars = {}
    tool.prediction = None
    tool.git_provider = provider or _provider()
    stub = _model_stub(prediction, fail_primary)

    async def prepare(model):
        tool.prediction = await stub(model)

    tool._prepare_prediction = prepare
    return tool


async def test_ask_appends_run_details_once_to_plain_comment():
    tool = _make_ask()

    await tool.run()

    [body] = _published_bodies(tool.git_provider)
    assert "### **Answer:**\nThe change is safe." in body
    assert body.count(TITLE) == 1
    assert f"Model: {PRIMARY}" in body
    assert "(fallback)" not in body


async def test_ask_threaded_reply_reports_the_fallback_model():
    provider = _provider()
    provider.supports_threaded_pr_questions.return_value = True
    get_settings().set("comment_id", 42)
    tool = _make_ask(fail_primary=True, provider=provider)

    await tool.run()

    comment_id, body = provider.reply_to_comment_from_comment_id.call_args.args
    assert comment_id == 42
    assert body.count(TITLE) == 1
    assert f"Model: {BACKUP} (fallback)" in body
    assert _published_bodies(provider) == [body]


async def test_ask_uses_plain_text_section_without_gfm():
    tool = _make_ask(provider=_provider(gfm=False))

    await tool.run()

    [body] = _published_bodies(tool.git_provider)
    assert f"**{TITLE}**" in body
    assert "<details>" not in body


async def test_ask_omits_run_details_when_disabled():
    get_settings().set("config.output_run_details", False)
    tool = _make_ask()

    await tool.run()

    [body] = _published_bodies(tool.git_provider)
    assert TITLE not in body


async def test_ask_keeps_unpublished_answer_free_of_run_details():
    get_settings().set("config.publish_output", False)
    tool = _make_ask()

    await tool.run()

    assert get_settings().get("data.answer") == "The change is safe."
    assert _published_bodies(tool.git_provider) == []


async def test_ask_starts_a_fresh_run_context():
    _seed_stale_fallback_run()
    tool = _make_ask()

    await tool.run()

    [body] = _published_bodies(tool.git_provider)
    assert f"Model: {PRIMARY}" in body
    assert "(fallback)" not in body
    assert "stale-model" not in body


# ---------------------------------------------------------------- /ask_line

HUNK = "@@ -1,2 +1,2 @@\n-old = 1\n+new = 1\n keep = 2\n"


def _make_ask_line(answer, fail_primary=False, provider=None, resolve_threads=False):
    settings = get_settings()
    settings.set("pr_questions.use_conversation_history", False)
    settings.set("ask_diff_hunk", HUNK)
    settings.set("file_name", "a.py")
    settings.set("line_start", 1)
    settings.set("line_end", 1)
    settings.set("side", "RIGHT")
    tool = PR_LineQuestions.__new__(PR_LineQuestions)
    tool.question_str = "Why rename this?"
    tool.ai_handler = None
    tool._ai_handler_factory = MagicMock(return_value=MagicMock())
    tool.main_pr_language = "python"
    tool.git_provider = provider or _provider()
    tool.resolve_threads = resolve_threads
    tool.vars = {"resolve_threads": resolve_threads, "conversation_history": ""}
    tool._get_prediction = _model_stub(answer, fail_primary)
    return tool


async def test_ask_line_reply_appends_run_details_after_sanitizing_and_resolving():
    provider = _provider()
    provider.resolve_comment_thread.return_value = True
    get_settings().set("comment_id", 7)
    tool = _make_ask_line("Renamed for clarity.\n/approve\n[THREAD_RESOLVED]",
                          provider=provider, resolve_threads=True)

    await tool.run()

    comment_id, body = provider.reply_to_comment_from_comment_id.call_args.args
    assert comment_id == 7
    assert body.startswith("Renamed for clarity.\n /approve")
    assert body.count(TITLE) == 1
    assert f"Model: {PRIMARY}" in body
    assert "[THREAD_RESOLVED]" not in body
    assert not any(line.startswith("/") for line in body.splitlines())
    provider.resolve_comment_thread.assert_called_once_with(7)


async def test_ask_line_plain_comment_reports_the_fallback_model():
    tool = _make_ask_line("Renamed for clarity.", fail_primary=True)

    await tool.run()

    [body] = _published_bodies(tool.git_provider)
    assert body.startswith("Renamed for clarity.")
    assert body.count(TITLE) == 1
    assert f"Model: {BACKUP} (fallback)" in body
    tool._ai_handler_factory.assert_called_once_with()


async def test_ask_line_omits_run_details_when_disabled():
    get_settings().set("config.output_run_details", False)
    tool = _make_ask_line("Renamed for clarity.")

    await tool.run()

    assert _published_bodies(tool.git_provider) == ["Renamed for clarity."]


async def test_ask_line_without_a_matching_hunk_has_no_run_details():
    _seed_stale_fallback_run()
    tool = _make_ask_line("unused")
    get_settings().set("ask_diff_hunk", "")
    tool.git_provider.get_diff_files.return_value = []

    await tool.run()

    [body] = _published_bodies(tool.git_provider)
    assert body.startswith("Could not find the requested lines")
    assert TITLE not in body
    assert tool._get_prediction.attempts == []
    tool._ai_handler_factory.assert_not_called()
