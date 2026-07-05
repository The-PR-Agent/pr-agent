"""Unit tests for pr_agent/tools/pr_line_questions.py (PR_LineQuestions).

Complements tests/unittest/test_pr_questions_helpers.py (which covers
``parse_args`` and ``_load_conversation_history``). Here we cover:

- ``run()``'s line-extraction path, exercising the real
  ``extract_hunk_lines_from_patch`` against a canned diff hunk
- answer sanitization (leading "/" must not look like a slash command)
- routing between ``reply_to_comment_from_comment_id`` and ``publish_comment``
- ``_get_prediction``'s prompt-variable building (rendered Jinja prompts)

Instances are built with ``__new__``; the model call is replaced by patching
``retry_with_fallback_models`` in the tool module (no network, no LLM).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_line_questions import PR_LineQuestions
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

CANNED_PATCH = (
    "@@ -1,3 +1,4 @@\n"
    " line one\n"
    "+added line\n"
    " line two\n"
    " line three"
)


def _make_tool(question_str="what does this do?", git_provider=None) -> PR_LineQuestions:
    obj = PR_LineQuestions.__new__(PR_LineQuestions)
    obj.question_str = question_str
    obj.git_provider = git_provider if git_provider is not None else MagicMock()
    obj.vars = {
        "title": "My PR title",
        "branch": "feature/some-branch",
        "diff": "",
        "question": question_str,
        "full_hunk": "",
        "selected_lines": "",
        "conversation_history": "",
        "extra_instructions": "",
    }
    obj.patches_diff = None
    obj.prediction = None
    return obj


@pytest.fixture
def line_settings():
    keys = (
        "ask_diff_hunk",
        "line_start",
        "line_end",
        "side",
        "file_name",
        "comment_id",
        "pr_questions.use_conversation_history",
    )
    saved = snapshot_settings(keys)
    settings = get_settings()
    settings.set("ask_diff_hunk", CANNED_PATCH)
    settings.set("file_name", "src/example.py")
    settings.set("line_start", 2)
    settings.set("line_end", 2)
    settings.set("side", "RIGHT")
    settings.set("comment_id", "")
    try:
        yield settings
    finally:
        restore_settings(saved)


def _patch_model_answer(monkeypatch, answer):
    """Replace retry_with_fallback_models in the tool module with a canned answer."""
    calls = []

    async def fake_retry(func, model_type=None):
        calls.append((func, model_type))
        return answer

    monkeypatch.setattr(
        "pr_agent.tools.pr_line_questions.retry_with_fallback_models", fake_retry
    )
    return calls


class TestRunLineExtraction:
    async def test_extracts_hunk_and_selected_lines_from_ask_diff(self, monkeypatch, line_settings):
        _patch_model_answer(monkeypatch, "an answer")
        tool = _make_tool()

        await tool.run()

        assert "## File: 'src/example.py'" in tool.patch_with_lines
        assert "@@ -1,3 +1,4 @@" in tool.patch_with_lines
        assert "+added line" in tool.patch_with_lines
        # line 2 on the RIGHT side is the added line
        assert tool.selected_lines == "+added line"

    async def test_falls_back_to_diff_files_when_no_ask_diff(self, monkeypatch, line_settings):
        line_settings.set("ask_diff_hunk", "")
        calls = _patch_model_answer(monkeypatch, "an answer")

        provider = MagicMock()
        provider.get_diff_files.return_value = [
            SimpleNamespace(filename="other/file.py", patch="@@ -1,1 +1,1 @@\n+other"),
            SimpleNamespace(filename="src/example.py", patch=CANNED_PATCH),
        ]
        tool = _make_tool(git_provider=provider)

        await tool.run()

        assert len(calls) == 1
        assert tool.selected_lines == "+added line"
        assert "## File: 'src/example.py'" in tool.patch_with_lines

    async def test_no_matching_file_skips_model_and_publish(self, monkeypatch, line_settings):
        line_settings.set("ask_diff_hunk", "")
        calls = _patch_model_answer(monkeypatch, "should not be used")

        provider = MagicMock()
        provider.get_diff_files.return_value = [
            SimpleNamespace(filename="other/file.py", patch=CANNED_PATCH),
        ]
        tool = _make_tool(git_provider=provider)

        result = await tool.run()

        assert result == ""
        assert calls == []
        provider.publish_comment.assert_not_called()
        provider.reply_to_comment_from_comment_id.assert_not_called()


class TestRunAnswerPublishing:
    async def test_publishes_comment_when_no_comment_id(self, monkeypatch, line_settings):
        _patch_model_answer(monkeypatch, "looks fine")
        tool = _make_tool()

        await tool.run()

        tool.git_provider.publish_comment.assert_called_once_with("looks fine")
        tool.git_provider.reply_to_comment_from_comment_id.assert_not_called()

    async def test_replies_to_comment_when_comment_id_set(self, monkeypatch, line_settings):
        line_settings.set("comment_id", 12345)
        _patch_model_answer(monkeypatch, "threaded answer")
        tool = _make_tool()

        await tool.run()

        tool.git_provider.reply_to_comment_from_comment_id.assert_called_once_with(
            12345, "threaded answer"
        )
        tool.git_provider.publish_comment.assert_not_called()

    async def test_sanitizes_leading_slash_in_answer(self, monkeypatch, line_settings):
        _patch_model_answer(monkeypatch, "/approve this now")
        tool = _make_tool()

        await tool.run()

        tool.git_provider.publish_comment.assert_called_once_with(" /approve this now")

    async def test_sanitizes_newline_slash_in_answer(self, monkeypatch, line_settings):
        _patch_model_answer(monkeypatch, "first line\n/close please")
        tool = _make_tool()

        await tool.run()

        tool.git_provider.publish_comment.assert_called_once_with("first line\n /close please")


class TestGetPrediction:
    class FakeAiHandler:
        def __init__(self):
            self.captured = {}

        async def chat_completion(self, model, temperature, system, user):
            self.captured = {
                "model": model,
                "temperature": temperature,
                "system": system,
                "user": user,
            }
            return "the model answer", "stop"

    async def test_prompt_vars_are_rendered_into_user_prompt(self):
        tool = _make_tool(question_str="why was this line added?")
        tool.ai_handler = self.FakeAiHandler()
        tool.patch_with_lines = "## File: 'src/example.py'\n\n@@ -1,3 +1,4 @@\n+added line"
        tool.selected_lines = "+added line"

        answer = await tool._get_prediction("some-model")

        assert answer == "the model answer"
        captured = tool.ai_handler.captured
        assert captured["model"] == "some-model"
        user_prompt = captured["user"]
        assert "Title: 'My PR title'" in user_prompt
        assert "Branch: 'feature/some-branch'" in user_prompt
        assert "## File: 'src/example.py'" in user_prompt
        assert "+added line" in user_prompt
        assert "why was this line added?" in user_prompt
        # no conversation history was provided -> the block must be omitted
        assert "Previous discussion on this code" not in user_prompt

    async def test_conversation_history_is_rendered_when_present(self):
        tool = _make_tool()
        tool.ai_handler = self.FakeAiHandler()
        tool.patch_with_lines = "@@ -1,1 +1,1 @@\n+x"
        tool.selected_lines = "+x"
        tool.vars["conversation_history"] = "1. alice: first comment\n2. bob: second comment"

        await tool._get_prediction("some-model")

        user_prompt = tool.ai_handler.captured["user"]
        assert "Previous discussion on this code" in user_prompt
        assert "1. alice: first comment" in user_prompt

    async def test_extra_instructions_are_rendered_into_system_prompt(self):
        tool = _make_tool()
        tool.ai_handler = self.FakeAiHandler()
        tool.patch_with_lines = "@@ -1,1 +1,1 @@\n+x"
        tool.selected_lines = "+x"
        tool.vars["extra_instructions"] = "Answer in French."

        await tool._get_prediction("some-model")

        system_prompt = tool.ai_handler.captured["system"]
        assert "Answer in French." in system_prompt
        assert "Extra instructions from the user" in system_prompt
