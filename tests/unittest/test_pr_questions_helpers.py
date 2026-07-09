"""Focused unit tests for PRQuestions / PR_LineQuestions pure helpers.

These tests avoid constructing the tool objects through their public
``__init__`` (which would create real git providers and a TokenHandler).
Instead, instances are built with ``__new__`` and only the attributes needed
by the method under test are populated. No live providers and no AI calls.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.gitlab_provider import GitLabProvider
from pr_agent.tools.pr_line_questions import PR_LineQuestions
from pr_agent.tools.pr_questions import PRQuestions
from tests.unittest._settings_helpers import SENTINEL, restore_settings, snapshot_settings


def _render_jinja_template(template: str, variables: dict) -> str:
    from jinja2 import Environment, StrictUndefined

    environment = Environment(undefined=StrictUndefined, autoescape=True)
    return environment.from_string(template).render(variables)


def _make_pr_questions(question_str: str = "", prediction: str = "", git_provider=None) -> PRQuestions:
    obj = PRQuestions.__new__(PRQuestions)
    obj.question_str = question_str
    obj.prediction = prediction
    obj.vars = {}
    obj.git_provider = git_provider if git_provider is not None else MagicMock()
    return obj


def _make_line_questions() -> PR_LineQuestions:
    obj = PR_LineQuestions.__new__(PR_LineQuestions)
    obj.vars = {}
    obj.git_provider = MagicMock()
    return obj


# ---------------------------------------------------------------------------
# PRQuestions.parse_args
# ---------------------------------------------------------------------------

class TestPRQuestionsParseArgs:
    def test_joins_multiple_args(self):
        pr = _make_pr_questions()
        assert pr.parse_args(["why", "is", "the", "sky", "blue?"]) == "why is the sky blue?"

    def test_empty_args_returns_empty_string(self):
        pr = _make_pr_questions()
        assert pr.parse_args([]) == ""
        assert pr.parse_args(None) == ""

    def test_single_arg(self):
        pr = _make_pr_questions()
        assert pr.parse_args(["hello"]) == "hello"


# ---------------------------------------------------------------------------
# PRQuestions.identify_image_in_comment
# ---------------------------------------------------------------------------

class TestIdentifyImageInComment:
    def test_markdown_image_extracts_url_and_sets_vars(self):
        pr = _make_pr_questions(
            question_str="explain this ![image](https://example.com/foo.png)"
        )
        result = pr.identify_image_in_comment()
        # Current contract: parses out content between the parentheses after
        # the literal "![image]" marker (strips surrounding parens).
        assert result == "https://example.com/foo.png"
        assert pr.vars["img_path"] == "https://example.com/foo.png"

    def test_direct_image_url_png(self):
        pr = _make_pr_questions(
            question_str="please look at https://example.com/diagram.png and answer"
        )
        result = pr.identify_image_in_comment()
        # Current behavior captures everything from "https://" to end of string
        # (including any trailing text). We assert the prefix / contains the URL,
        # rather than the exact full match, to remain robust to that quirk.
        assert result.startswith("https://example.com/diagram.png")
        assert pr.vars["img_path"] == result

    def test_direct_image_url_jpg(self):
        pr = _make_pr_questions(
            question_str="see https://example.com/screen.jpg"
        )
        result = pr.identify_image_in_comment()
        assert result.startswith("https://example.com/screen.jpg")
        assert "img_path" in pr.vars

    def test_no_image_returns_empty_and_does_not_set_vars(self):
        pr = _make_pr_questions(question_str="just a plain text question")
        result = pr.identify_image_in_comment()
        assert result == ""
        assert "img_path" not in pr.vars

    def test_https_without_image_extension_returns_empty(self):
        pr = _make_pr_questions(question_str="see https://example.com/docs.html")
        result = pr.identify_image_in_comment()
        assert result == ""
        assert "img_path" not in pr.vars


# ---------------------------------------------------------------------------
# PRQuestions._prepare_pr_answer
# ---------------------------------------------------------------------------

class TestPreparePrAnswer:
    def test_wraps_answer_with_ask_answer_headers(self):
        pr = _make_pr_questions(
            question_str="why?",
            prediction="because reasons",
            git_provider=MagicMock(),  # not GitLab
        )
        out = pr._prepare_pr_answer()
        assert "### **Ask**❓" in out
        assert "why?" in out
        assert "### **Answer:**" in out
        assert "because reasons" in out

    def test_sanitizes_leading_slash(self):
        pr = _make_pr_questions(
            question_str="q", prediction="/merge looks fine", git_provider=MagicMock()
        )
        out = pr._prepare_pr_answer()
        # Leading "/" should have been prefixed with a space so the answer
        # does not look like a slash command to the host platform.
        assert "\n /merge looks fine" in out
        assert "\n/merge" not in out

    def test_sanitizes_newline_slash(self):
        pr = _make_pr_questions(
            question_str="q", prediction="hello\n/close now", git_provider=MagicMock()
        )
        out = pr._prepare_pr_answer()
        assert "\n /close now" in out
        assert "\n/close" not in out

    def test_sanitizes_carriage_return_slash(self):
        pr = _make_pr_questions(
            question_str="q", prediction="hello\r/close", git_provider=MagicMock()
        )
        out = pr._prepare_pr_answer()
        assert "\r /close" in out
        assert "\r/close" not in out

    def test_non_gitlab_provider_does_not_apply_gitlab_protections(self):
        # Use a non-GitLab provider; a model answer that *does* contain a
        # quick-action substring like "/merge" must still come through as a
        # (sanitized) answer, NOT be replaced with the GitLab error string.
        pr = _make_pr_questions(
            question_str="q", prediction="/merge would be premature", git_provider=MagicMock()
        )
        out = pr._prepare_pr_answer()
        assert "Model answer contains GitHub quick actions" not in out
        assert "would be premature" in out

    def test_gitlab_provider_blocks_quick_actions(self):
        gitlab_provider = GitLabProvider.__new__(GitLabProvider)
        pr = _make_pr_questions(
            question_str="q",
            prediction="/merge this please",
            git_provider=gitlab_provider,
        )
        out = pr._prepare_pr_answer()
        assert "Model answer contains GitHub quick actions" in out

    def test_gitlab_provider_passes_through_safe_text(self):
        gitlab_provider = GitLabProvider.__new__(GitLabProvider)
        pr = _make_pr_questions(
            question_str="q",
            prediction="this change looks correct",
            git_provider=gitlab_provider,
        )
        out = pr._prepare_pr_answer()
        assert "this change looks correct" in out
        assert "Model answer contains GitHub quick actions" not in out


# ---------------------------------------------------------------------------
# PRQuestions.gitlab_protections
# ---------------------------------------------------------------------------

class TestGitlabProtections:
    @pytest.mark.parametrize(
        "quick_action",
        ["/approve", "/close", "/merge", "/reopen", "/unapprove",
         "/title", "/assign", "/copy_metadata", "/target_branch"],
    )
    def test_detects_each_quick_action(self, quick_action):
        pr = _make_pr_questions()
        result = pr.gitlab_protections(f"prefix {quick_action} suffix")
        assert "GitHub quick actions" in result

    def test_passthrough_for_safe_text(self):
        pr = _make_pr_questions()
        safe = "everything is fine here"
        assert pr.gitlab_protections(safe) == safe


# ---------------------------------------------------------------------------
# PR_LineQuestions.parse_args
# ---------------------------------------------------------------------------

class TestLineQuestionsParseArgs:
    def test_joins_multiple_args(self):
        lq = _make_line_questions()
        assert lq.parse_args(["what", "does", "this", "do"]) == "what does this do"

    def test_empty_args(self):
        lq = _make_line_questions()
        assert lq.parse_args([]) == ""
        assert lq.parse_args(None) == ""


# ---------------------------------------------------------------------------
# PR_LineQuestions._load_conversation_history
# ---------------------------------------------------------------------------

@pytest.fixture
def line_question_settings():
    """Snapshot and restore the dynaconf keys touched by these tests.

    Uses a SENTINEL-based snapshot so keys that were originally absent are
    truly removed during teardown, rather than being restored as ``None``.
    """
    settings = get_settings()
    keys = ("comment_id", "file_name", "line_end")
    saved = snapshot_settings(keys)
    try:
        yield settings
    finally:
        restore_settings(saved)


class TestLoadConversationHistory:
    def _set_required(self, settings, *, comment_id=42, file_name="src/foo.py", line_end=10):
        settings.set("comment_id", comment_id)
        settings.set("file_name", file_name)
        settings.set("line_end", line_end)

    def test_returns_empty_when_settings_missing(self, line_question_settings):
        # explicitly clear all required settings
        line_question_settings.set("comment_id", "")
        line_question_settings.set("file_name", "")
        line_question_settings.set("line_end", "")

        lq = _make_line_questions()
        # provider should not be consulted at all
        lq.git_provider.get_review_thread_comments = MagicMock(
            side_effect=AssertionError("provider must not be called")
        )
        assert lq._load_conversation_history() == ""

    def test_returns_empty_when_only_one_required_setting_missing(self, line_question_settings):
        line_question_settings.set("comment_id", 7)
        line_question_settings.set("file_name", "")  # missing
        line_question_settings.set("line_end", 5)

        lq = _make_line_questions()
        lq.git_provider.get_review_thread_comments = MagicMock(
            side_effect=AssertionError("provider must not be called")
        )
        assert lq._load_conversation_history() == ""

    def test_filters_empty_and_current_comment_and_formats(self, line_question_settings):
        self._set_required(line_question_settings, comment_id=100)

        current = SimpleNamespace(id=100, body="this is the current comment",
                                  user=SimpleNamespace(login="alice"))
        empty = SimpleNamespace(id=101, body="", user=SimpleNamespace(login="bob"))
        whitespace = SimpleNamespace(id=102, body="   \n  ",
                                     user=SimpleNamespace(login="carol"))
        good1 = SimpleNamespace(id=103, body="first reply",
                                user=SimpleNamespace(login="dave"))
        good2 = SimpleNamespace(id=104, body="second reply",
                                user=SimpleNamespace(login="erin"))

        lq = _make_line_questions()
        lq.git_provider.get_review_thread_comments = MagicMock(
            return_value=[current, empty, whitespace, good1, good2]
        )

        out = lq._load_conversation_history()
        assert out == "1. dave: first reply\n2. erin: second reply"

    def test_user_without_login_attribute_is_unknown(self, line_question_settings):
        self._set_required(line_question_settings, comment_id=1)

        # user object that has no 'login' attribute at all
        class _NoLoginUser:
            pass

        comment = SimpleNamespace(id=2, body="anonymous reply", user=_NoLoginUser())

        lq = _make_line_questions()
        lq.git_provider.get_review_thread_comments = MagicMock(return_value=[comment])

        out = lq._load_conversation_history()
        assert out == "1. Unknown: anonymous reply"

    def test_provider_exception_returns_empty_without_raising(self, line_question_settings):
        self._set_required(line_question_settings, comment_id=1)

        lq = _make_line_questions()
        lq.git_provider.get_review_thread_comments = MagicMock(
            side_effect=RuntimeError("boom")
        )

        # must not propagate the exception
        assert lq._load_conversation_history() == ""

    def test_only_filtered_comments_returns_empty(self, line_question_settings):
        self._set_required(line_question_settings, comment_id=10)

        # everything in the thread is either the current comment or empty
        current = SimpleNamespace(id=10, body="current", user=SimpleNamespace(login="u"))
        blank = SimpleNamespace(id=11, body="", user=SimpleNamespace(login="u"))

        lq = _make_line_questions()
        lq.git_provider.get_review_thread_comments = MagicMock(
            return_value=[current, blank]
        )
        assert lq._load_conversation_history() == ""


def test_line_question_settings_teardown_restores_sentinel_for_missing_keys():
    """Run the fixture manually and verify keys absent before are absent after."""
    settings = get_settings()
    key = "comment_id"
    # Make sure key is genuinely absent on entry.
    if settings.get(key, SENTINEL) is not SENTINEL:
        restore_settings({key: SENTINEL})
    assert settings.get(key, SENTINEL) is SENTINEL

    saved = snapshot_settings((key,))
    try:
        settings.set(key, 999)
        assert settings.get(key) == 999
    finally:
        restore_settings(saved)

    assert settings.get(key, SENTINEL) is SENTINEL


# ---------------------------------------------------------------------------
# extra_instructions prompt rendering
# ---------------------------------------------------------------------------

class TestExtraInstructionsPromptRendering:
    @pytest.fixture
    def extra_instructions_settings(self):
        keys = ("pr_questions.extra_instructions",)
        saved = snapshot_settings(keys)
        try:
            yield get_settings()
        finally:
            restore_settings(saved)

    def test_ask_system_prompt_includes_extra_instructions_when_set(self, extra_instructions_settings):
        extra_instructions_settings.set(
            "pr_questions.extra_instructions",
            "Do not answer questions that ask to rate PR quality.",
        )
        variables = {"extra_instructions": get_settings().pr_questions.extra_instructions}
        system_prompt = _render_jinja_template(get_settings().pr_questions_prompt.system, variables)
        assert "Do not answer questions that ask to rate PR quality." in system_prompt
        assert "take precedence over any conflicting guidance" in system_prompt

    def test_ask_system_prompt_omits_extra_instructions_block_when_empty(self, extra_instructions_settings):
        extra_instructions_settings.set("pr_questions.extra_instructions", "")
        variables = {"extra_instructions": get_settings().pr_questions.extra_instructions}
        system_prompt = _render_jinja_template(get_settings().pr_questions_prompt.system, variables)
        assert "Extra instructions from the user" not in system_prompt

    def test_ask_line_system_prompt_includes_extra_instructions_when_set(self, extra_instructions_settings):
        extra_instructions_settings.set(
            "pr_questions.extra_instructions",
            "Do not answer questions that ask to rate PR quality.",
        )
        variables = {"extra_instructions": get_settings().pr_questions.extra_instructions}
        system_prompt = _render_jinja_template(get_settings().pr_line_questions_prompt.system, variables)
        assert "Do not answer questions that ask to rate PR quality." in system_prompt
        assert "take precedence over any conflicting guidance" in system_prompt
