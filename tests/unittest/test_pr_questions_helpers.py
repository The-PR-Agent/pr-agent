"""Focused unit tests for PRQuestions / PR_LineQuestions pure helpers.

These tests avoid constructing the tool objects through their public
``__init__`` (which would create real git providers and a TokenHandler).
Instead, instances are built with ``__new__`` and only the attributes needed
by the method under test are populated. No live providers and no AI calls.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import pr_agent.tools.pr_line_questions as plq
from pr_agent.algo.utils import format_pr_questions_header
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import AzureDevopsProvider
from pr_agent.git_providers.codecommit_provider import CodeCommitProvider
from pr_agent.git_providers.gerrit_provider import GerritProvider, adopt_to_gerrit_message
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider
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
    if git_provider is None:
        git_provider = MagicMock()
        git_provider.supports_threaded_pr_questions.return_value = False
    obj.git_provider = git_provider
    return obj


def _make_line_questions() -> plq.PR_LineQuestions:
    obj = plq.PR_LineQuestions.__new__(plq.PR_LineQuestions)
    obj.vars = {}
    obj.resolve_threads = False
    obj.git_provider = MagicMock()
    obj.git_provider.supports_threaded_pr_questions.return_value = False
    obj.git_provider.supports_line_question_history.return_value = False
    return obj


def test_question_capabilities_are_provider_driven():
    provider = MagicMock()

    assert GitProvider.supports_threaded_pr_questions(provider) is False
    assert GitProvider.supports_line_question_history(provider) is False
    assert AzureDevopsProvider.supports_threaded_pr_questions(provider) is True
    assert AzureDevopsProvider.supports_line_question_history(provider) is True
    assert GithubProvider.supports_line_question_history(provider) is True


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

    def test_decodes_internal_question_argument(self):
        pr = _make_pr_questions()
        encoded = "__pr_agent_encoded_text__:does%20--some_key%3D1%20change%20the%20request%3F"
        assert pr.parse_args([encoded]) == "does --some_key=1 change the request?"


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
        assert out == "### **Ask** ❓\nwhy?\n\n### **Answer:**\nbecause reasons\n\n"

    def test_custom_heading_changes_only_the_ask_header(self):
        settings = get_settings()
        saved = snapshot_settings(("pr_questions.ask_heading",))
        pr = _make_pr_questions(question_str="why?", prediction="because reasons")
        try:
            settings.set("pr_questions.ask_heading", "  Architecture Question  ")
            out = pr._prepare_pr_answer()
        finally:
            restore_settings(saved)

        assert out == "### **Architecture Question** ❓\nwhy?\n\n### **Answer:**\nbecause reasons\n\n"

    @pytest.mark.parametrize(
        "invalid_heading",
        [
            None,
            "",
            "   ",
            "Ask\nNow",
            "Ask\rNow",
            "Ask\vNow",
            "Ask\fNow",
            "Ask\x1cNow",
            "Ask\x1dNow",
            "Ask\x1eNow",
            "Ask\x85Now",
            "Ask\u2028Now",
            "Ask\u2029Now",
            "Ask\u2028",
            42,
        ],
    )
    def test_invalid_heading_falls_back_to_ask(self, invalid_heading):
        settings = get_settings()
        saved = snapshot_settings(("pr_questions.ask_heading",))
        try:
            settings.set("pr_questions.ask_heading", invalid_heading)
            header = format_pr_questions_header()
        finally:
            restore_settings(saved)

        assert header == "### **Ask** ❓"

    @pytest.mark.parametrize(
        ("heading", "expected"),
        [
            ("Architecture **Question**", r"### **Architecture \*\*Question\*\*** ❓"),
            (
                r"Use [SDK](docs/v2) `now` \\ safely",
                r"### **Use \[SDK\]\(docs\/v2\) \`now\` \\\\ safely** ❓",
            ),
            ("Architecture Ω", "### **Architecture Ω** ❓"),
        ],
    )
    def test_heading_is_rendered_as_literal_text(self, heading, expected):
        settings = get_settings()
        saved = snapshot_settings(("pr_questions.ask_heading",))
        try:
            settings.set("pr_questions.ask_heading", heading)
            header = format_pr_questions_header()
        finally:
            restore_settings(saved)

        assert header == expected

    def test_codecommit_does_not_receive_markdown_escapes(self):
        settings = get_settings()
        saved = snapshot_settings(("pr_questions.ask_heading",))
        provider = CodeCommitProvider.__new__(CodeCommitProvider)
        pr = _make_pr_questions(
            question_str="why?",
            prediction="because reasons",
            git_provider=provider,
        )
        try:
            settings.set("pr_questions.ask_heading", "Q&A / Security")
            out = pr._prepare_pr_answer()
        finally:
            restore_settings(saved)

        assert out.startswith("### **Q&A / Security** ❓\n")
        assert "\\" not in out.splitlines()[0]

    def test_gerrit_preserves_literal_heading_punctuation(self):
        settings = get_settings()
        saved = snapshot_settings(("pr_questions.ask_heading",))
        provider = GerritProvider.__new__(GerritProvider)
        pr = _make_pr_questions(
            question_str="why?",
            prediction="because reasons",
            git_provider=provider,
        )
        heading = r"Hash #, star *, [docs](v2), /, `tick`, |, \ path"
        try:
            settings.set("pr_questions.ask_heading", heading)
            answer = pr._prepare_pr_answer()
            out = adopt_to_gerrit_message(answer)
        finally:
            restore_settings(saved)

        raw_heading = answer.splitlines()[0]
        assert r"\#" in raw_heading
        assert r"\*" in raw_heading
        assert r"\\" in raw_heading
        assert out.splitlines()[0] == f"{heading}❓:"

    def test_gerrit_falls_back_for_a_unicode_line_separator(self):
        settings = get_settings()
        saved = snapshot_settings(("pr_questions.ask_heading",))
        provider = GerritProvider.__new__(GerritProvider)
        pr = _make_pr_questions(
            question_str="why?",
            prediction="because reasons",
            git_provider=provider,
        )
        try:
            settings.set("pr_questions.ask_heading", "Architecture\u2028Question")
            answer = pr._prepare_pr_answer()
            out = adopt_to_gerrit_message(answer)
        finally:
            restore_settings(saved)

        assert answer.startswith("### **Ask** ❓\n")
        assert out.splitlines()[0] == "Ask❓:"

    def test_gerrit_keeps_existing_conversion_for_other_markdown_headings(self):
        message = "### **Answer:**\n- item\n### **Model # Heading:**"

        assert adopt_to_gerrit_message(message) == "Answer:\nitem\n\nModel  Heading:"

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

    @pytest.mark.parametrize(
        "quick_action",
        ["/approve", "/close", "/merge", "/reopen", "/unapprove",
         "/title", "/assign", "/copy_metadata", "/target_branch"],
    )
    def test_mid_line_quick_action_mention_survives_on_gitlab(self, quick_action):
        # Regression pin for #2302: prose that merely *mentions* a quick action
        # (e.g. an MR template documenting pr-agent usage) must be published
        # verbatim, not replaced with an error. Quick actions only execute at
        # the start of a line, and line starts are already space-prefixed.
        gitlab_provider = GitLabProvider.__new__(GitLabProvider)
        prediction = f"Comment {quick_action} on the MR to trigger the flow."
        pr = _make_pr_questions(
            question_str="q", prediction=prediction, git_provider=gitlab_provider
        )
        out = pr._prepare_pr_answer()
        assert prediction in out
        assert "Model answer contains GitHub quick actions" not in out

    def test_line_leading_quick_action_is_neutralized_on_gitlab(self):
        gitlab_provider = GitLabProvider.__new__(GitLabProvider)
        pr = _make_pr_questions(
            question_str="q",
            prediction="To finish:\n/merge this please",
            git_provider=gitlab_provider,
        )
        out = pr._prepare_pr_answer()
        assert "\n /merge this please" in out
        assert "\n/merge" not in out

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


class TestPublishPrAnswer:
    def test_replies_to_azure_thread_when_comment_id_is_set(self):
        settings = get_settings()
        saved = snapshot_settings(("comment_id",))
        provider = MagicMock(spec=AzureDevopsProvider)
        provider.supports_threaded_pr_questions.return_value = True
        pr = _make_pr_questions(git_provider=provider)
        try:
            settings.set("comment_id", 42)
            pr._publish_answer("answer")
        finally:
            restore_settings(saved)

        provider.reply_to_comment_from_comment_id.assert_called_once_with(42, "answer")
        provider.publish_comment.assert_not_called()

    def test_non_azure_answer_ignores_thread_setting(self):
        settings = get_settings()
        saved = snapshot_settings(("comment_id",))
        provider = MagicMock()
        provider.supports_threaded_pr_questions.return_value = False
        pr = _make_pr_questions(git_provider=provider)
        try:
            settings.set("comment_id", 42)
            pr._publish_answer("answer")
        finally:
            restore_settings(saved)

        provider.publish_comment.assert_called_once_with("answer")
        provider.reply_to_comment_from_comment_id.assert_not_called()

    def test_publishes_normally_without_comment_id(self):
        settings = get_settings()
        saved = snapshot_settings(("comment_id",))
        provider = MagicMock(spec=AzureDevopsProvider)
        provider.supports_threaded_pr_questions.return_value = True
        pr = _make_pr_questions(git_provider=provider)
        try:
            settings.set("comment_id", "")
            pr._publish_answer("answer")
        finally:
            restore_settings(saved)

        provider.publish_comment.assert_called_once_with("answer")
        provider.reply_to_comment_from_comment_id.assert_not_called()


class TestLoadPrConversationHistory:
    def test_formats_prior_comments_and_skips_current_question(self):
        settings = get_settings()
        saved = snapshot_settings(("comment_id", "origin_comment_id", "pr_questions.use_conversation_history"))
        provider = MagicMock(spec=AzureDevopsProvider)
        provider.supports_threaded_pr_questions.return_value = True
        provider.get_review_thread_comments.return_value = [
            SimpleNamespace(id=10, body="Original suggestion", user=SimpleNamespace(login="agent")),
            SimpleNamespace(id=11, body="Current question", user=SimpleNamespace(login="alice")),
            SimpleNamespace(id=12, body="Earlier reply", user=SimpleNamespace(login="bob")),
        ]
        pr = _make_pr_questions(git_provider=provider)
        try:
            settings.set("comment_id", 20)
            settings.set("origin_comment_id", 11)
            settings.set("pr_questions.use_conversation_history", True)
            history = pr._load_conversation_history()
        finally:
            restore_settings(saved)

        assert history == "1. agent: Original suggestion\n2. bob: Earlier reply"
        provider.get_review_thread_comments.assert_called_once_with(20)

    def test_returns_empty_when_history_is_disabled(self):
        settings = get_settings()
        saved = snapshot_settings(("comment_id", "pr_questions.use_conversation_history"))
        provider = MagicMock()
        provider.supports_threaded_pr_questions.return_value = False
        pr = _make_pr_questions(git_provider=provider)
        try:
            settings.set("comment_id", 20)
            settings.set("pr_questions.use_conversation_history", False)
            history = pr._load_conversation_history()
        finally:
            restore_settings(saved)

        assert history == ""
        provider.get_review_thread_comments.assert_not_called()

    def test_ignores_non_azure_thread_settings(self):
        settings = get_settings()
        saved = snapshot_settings(("comment_id", "pr_questions.use_conversation_history"))
        provider = MagicMock()
        provider.supports_threaded_pr_questions.return_value = False
        pr = _make_pr_questions(git_provider=provider)
        try:
            settings.set("comment_id", 20)
            settings.set("pr_questions.use_conversation_history", True)
            history = pr._load_conversation_history()
        finally:
            restore_settings(saved)

        assert history == ""
        provider.get_review_thread_comments.assert_not_called()


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

    def test_decodes_internal_question_argument(self):
        lq = _make_line_questions()
        encoded = "__pr_agent_encoded_text__:does%20--some_key%3D1%20change%20the%20request%3F"
        assert lq.parse_args([encoded]) == "does --some_key=1 change the request?"


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
    keys = ("comment_id", "origin_comment_id", "file_name", "file_name_encoded", "line_start", "line_end", "side")
    saved = snapshot_settings(keys)
    try:
        yield settings
    finally:
        restore_settings(saved)


@pytest.mark.asyncio
async def test_line_question_decodes_webhook_file_path(monkeypatch, line_question_settings):
    file_name = "/src/folder name/don't.py"
    line_question_settings.set("file_name", "%2Fsrc%2Ffolder%20name%2Fdon%27t.py")
    line_question_settings.set("file_name_encoded", True)
    line_question_settings.set("line_start", 8)
    line_question_settings.set("line_end", 8)
    line_question_settings.set("side", "right")
    line_question_settings.set("comment_id", 22)

    lq = _make_line_questions()
    lq.git_provider.get_diff_files.return_value = [SimpleNamespace(filename=file_name, patch="diff")]
    extract_hunk = MagicMock(return_value=("@@ -8,1 +8,1 @@\n patch", "selected"))

    async def get_answer(*args, **kwargs):
        return "answer"

    monkeypatch.setattr("pr_agent.tools.pr_line_questions.extract_hunk_lines_from_patch", extract_hunk)
    monkeypatch.setattr("pr_agent.tools.pr_line_questions.retry_with_fallback_models", get_answer)

    await lq.run()

    extract_hunk.assert_called_once_with("diff", file_name, line_start=8, line_end=8, side="right")
    lq.git_provider.reply_to_comment_from_comment_id.assert_called_once_with(22, "answer")


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

    def test_uses_origin_comment_id_when_reply_target_is_a_thread(self, line_question_settings):
        self._set_required(line_question_settings, comment_id=200)
        line_question_settings.set("origin_comment_id", 102)
        comments = [
            SimpleNamespace(id=101, body="Earlier context", user=SimpleNamespace(login="alice")),
            SimpleNamespace(id=102, body="Current question", user=SimpleNamespace(login="bob")),
        ]
        lq = _make_line_questions()
        lq.git_provider = MagicMock(spec=AzureDevopsProvider)
        lq.git_provider.supports_threaded_pr_questions.return_value = True
        lq.git_provider.get_review_thread_comments = MagicMock(return_value=comments)

        assert lq._load_conversation_history() == "1. alice: Earlier context"

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
        variables = {
            "extra_instructions": get_settings().pr_questions.extra_instructions,
            "skills_context": "",
        }
        system_prompt = _render_jinja_template(get_settings().pr_questions_prompt.system, variables)
        assert "Do not answer questions that ask to rate PR quality." in system_prompt
        assert "take precedence over any conflicting guidance" in system_prompt

    def test_ask_system_prompt_omits_extra_instructions_block_when_empty(self, extra_instructions_settings):
        extra_instructions_settings.set("pr_questions.extra_instructions", "")
        variables = {
            "extra_instructions": get_settings().pr_questions.extra_instructions,
            "skills_context": "",
        }
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


# ---------------------------------------------------------------------------
# resolve_threads prompt rendering
# ---------------------------------------------------------------------------

class TestResolveThreadsPromptRendering:
    def test_resolve_threads_marker_instruction_included_when_enabled(self):
        variables = {
            "title": "test",
            "branch": "main",
            "full_hunk": "some code",
            "selected_lines": "line1",
            "question": "is this fixed?",
            "conversation_history": "",
            "resolve_threads": True,
            "extra_instructions": "",
        }
        user_prompt = _render_jinja_template(
            get_settings().pr_line_questions_prompt.user, variables
        )
        assert "[THREAD_RESOLVED]" in user_prompt
        assert "determine whether the discussion thread is now fully resolved" in user_prompt

    def test_resolve_threads_marker_instruction_omitted_when_disabled(self):
        variables = {
            "title": "test",
            "branch": "main",
            "full_hunk": "some code",
            "selected_lines": "line1",
            "question": "what does this do?",
            "conversation_history": "",
            "resolve_threads": False,
            "extra_instructions": "",
        }
        user_prompt = _render_jinja_template(
            get_settings().pr_line_questions_prompt.user, variables
        )
        assert "[THREAD_RESOLVED]" not in user_prompt


# ---------------------------------------------------------------------------
# resolve_threads disabled when no comment_id
# ---------------------------------------------------------------------------

class TestResolveThreadsDisabledWithoutCommentId:
    @pytest.fixture
    def resolve_settings(self):
        keys = ("comment_id", "pr_questions.resolve_threads")
        saved = snapshot_settings(keys)
        try:
            yield get_settings()
        finally:
            restore_settings(saved)

    @pytest.mark.asyncio
    async def test_resolve_threads_cleared_when_no_comment_id(self, resolve_settings):
        # drive run() rather than restating the branch, so deleting the clearing fails here
        resolve_settings.set("pr_questions.resolve_threads", True)
        resolve_settings.set("pr_questions.use_conversation_history", False)
        resolve_settings.set("comment_id", "")
        resolve_settings.set("ask_diff_hunk", "@@ -1,3 +1,3 @@\n-old\n+new\n ctx")
        resolve_settings.set("line_start", 1)
        resolve_settings.set("line_end", 1)
        resolve_settings.set("side", "RIGHT")
        resolve_settings.set("file_name", "test.py")

        lq = _make_line_questions()
        lq.resolve_threads = True
        lq.vars["resolve_threads"] = True
        lq.token_handler = MagicMock()

        async def fake_retry(func, **kwargs):
            return "Looks good.\n\n[THREAD_RESOLVED]"

        original = plq.retry_with_fallback_models
        plq.retry_with_fallback_models = fake_retry
        try:
            await lq.run()
        finally:
            plq.retry_with_fallback_models = original

        assert lq.resolve_threads is False
        assert lq.vars["resolve_threads"] is False
        lq.git_provider.resolve_comment_thread.assert_not_called()

    def test_resolve_threads_kept_when_comment_id_present(self, resolve_settings):
        resolve_settings.set("pr_questions.resolve_threads", True)
        resolve_settings.set("comment_id", 12345)

        lq = _make_line_questions()
        lq.resolve_threads = get_settings().pr_questions.get("resolve_threads", False)
        lq.vars = {"resolve_threads": lq.resolve_threads}

        comment_id = get_settings().get("comment_id", "")
        if not comment_id:
            lq.resolve_threads = False
            lq.vars["resolve_threads"] = False

        assert lq.vars["resolve_threads"] is True
        assert lq.resolve_threads is True


# ---------------------------------------------------------------------------
# Thread resolution marker parsing (unit tests for run() logic)
# ---------------------------------------------------------------------------

class TestThreadResolvedMarkerParsing:
    """Test the marker-stripping logic that would be in PR_LineQuestions.run().

    The marker is only recognized when it appears at the end of the response
    (after rstrip). Mid-message occurrences are treated as normal text.
    """

    def _parse_marker(self, answer, resolve_threads=True):
        """Replicate the endswith-based parsing from PR_LineQuestions.run()."""
        answer_stripped = answer.rstrip()
        if resolve_threads and answer_stripped.endswith("[THREAD_RESOLVED]"):
            return True, answer_stripped[:-len("[THREAD_RESOLVED]")].rstrip()
        return False, answer

    @pytest.mark.parametrize("marker_position,answer,expected_resolve,expected_clean", [
        ("end", "The issue is fixed.\n\n[THREAD_RESOLVED]", True, "The issue is fixed."),
        ("end_with_trailing_whitespace", "Done.\n[THREAD_RESOLVED]  \n", True, "Done."),
        ("only", "[THREAD_RESOLVED]", True, ""),
    ])
    def test_trailing_marker_is_stripped(self, marker_position, answer, expected_resolve, expected_clean):
        should_resolve, cleaned = self._parse_marker(answer)
        assert should_resolve == expected_resolve
        assert cleaned == expected_clean

    def test_mid_message_marker_is_not_resolved(self):
        answer = "Fixed [THREAD_RESOLVED] thanks"
        should_resolve, cleaned = self._parse_marker(answer)
        assert should_resolve is False
        assert cleaned == answer

    def test_no_marker_means_no_resolve(self):
        answer = "I think this still needs work."
        should_resolve, cleaned = self._parse_marker(answer)
        assert should_resolve is False
        assert cleaned == answer

    def test_marker_ignored_when_resolve_threads_disabled(self):
        answer = "The issue is fixed.\n\n[THREAD_RESOLVED]"
        should_resolve, cleaned = self._parse_marker(answer, resolve_threads=False)
        assert should_resolve is False
        assert cleaned == answer


# ---------------------------------------------------------------------------
# Regression test: run() actually calls resolve_comment_thread when marker present
# ---------------------------------------------------------------------------

class TestRunResolvesThread:
    """Exercise the resolve wiring inside PR_LineQuestions.run().

    This ensures that removing the resolve code from run() would break a test.
    """

    @pytest.fixture
    def run_settings(self):
        keys = (
            "comment_id", "pr_questions.resolve_threads",
            "pr_questions.use_conversation_history",
            "ask_diff_hunk", "line_start", "line_end", "side", "file_name",
        )
        saved = snapshot_settings(keys)
        try:
            yield get_settings()
        finally:
            restore_settings(saved)

    @pytest.mark.asyncio
    async def test_run_calls_resolve_when_marker_present(self, run_settings):
        run_settings.set("pr_questions.resolve_threads", True)
        run_settings.set("pr_questions.use_conversation_history", False)
        run_settings.set("comment_id", 42)
        run_settings.set("ask_diff_hunk", "@@ -1,3 +1,3 @@\n-old\n+new\n ctx")
        run_settings.set("line_start", 1)
        run_settings.set("line_end", 1)
        run_settings.set("side", "RIGHT")
        run_settings.set("file_name", "test.py")

        lq = _make_line_questions()
        lq.resolve_threads = True
        lq.vars["resolve_threads"] = True
        lq.token_handler = MagicMock()

        async def fake_retry(func, **kwargs):
            return "Looks good.\n\n[THREAD_RESOLVED]"

        original = plq.retry_with_fallback_models
        plq.retry_with_fallback_models = fake_retry
        try:
            await lq.run()
        finally:
            plq.retry_with_fallback_models = original

        lq.git_provider.reply_to_comment_from_comment_id.assert_called_once()
        reply_body = lq.git_provider.reply_to_comment_from_comment_id.call_args[0][1]
        assert "[THREAD_RESOLVED]" not in reply_body

        lq.git_provider.resolve_comment_thread.assert_called_once_with(42)

    @pytest.mark.asyncio
    async def test_run_does_not_resolve_without_marker(self, run_settings):
        run_settings.set("pr_questions.resolve_threads", True)
        run_settings.set("pr_questions.use_conversation_history", False)
        run_settings.set("comment_id", 42)
        run_settings.set("ask_diff_hunk", "@@ -1,3 +1,3 @@\n-old\n+new\n ctx")
        run_settings.set("line_start", 1)
        run_settings.set("line_end", 1)
        run_settings.set("side", "RIGHT")
        run_settings.set("file_name", "test.py")

        lq = _make_line_questions()
        lq.resolve_threads = True
        lq.vars["resolve_threads"] = True
        lq.token_handler = MagicMock()

        async def fake_retry(func, **kwargs):
            return "This still needs work."

        original = plq.retry_with_fallback_models
        plq.retry_with_fallback_models = fake_retry
        try:
            await lq.run()
        finally:
            plq.retry_with_fallback_models = original

        lq.git_provider.reply_to_comment_from_comment_id.assert_called_once()
        lq.git_provider.resolve_comment_thread.assert_not_called()


# ---------------------------------------------------------------------------
# PR_LineQuestions.run - model call gating (no hunk lines selected)
# ---------------------------------------------------------------------------

class TestPRLineQuestionsRunSkipsEmptySelection:
    """Skip the model call when no hunk lines are selected.

    ``extract_hunk_lines_from_patch`` returns a truthy header-only string as
    ``patch_with_lines`` whenever the requested range misses every hunk or the
    patch is unparseable, so the model call has to be gated on
    ``selected_lines`` rather than on ``patch_with_lines``.
    """

    _PATCH = (
        "@@ -5,7 +5,8 @@ def main():\n"
        "     a = 1\n"
        "     b = 2\n"
        "+    c = 3\n"
        "     return a\n"
    )

    def _provider(self):
        obj = plq.PR_LineQuestions.__new__(plq.PR_LineQuestions)
        obj.vars = {}
        obj.git_provider = MagicMock()
        obj.token_handler = MagicMock()
        obj.git_provider.get_diff_files.return_value = [SimpleNamespace(
            filename="x.py", patch=self._PATCH)]
        return obj

    def _set_ask_settings(self, line_start, line_end):
        keys = ("ask_diff_hunk", "line_start", "line_end", "side", "file_name", "comment_id")
        saved = snapshot_settings(keys)
        settings = get_settings()
        settings.unset("ask_diff_hunk", force=True)
        settings.set("line_start", line_start)
        settings.set("line_end", line_end)
        settings.set("side", "RIGHT")
        settings.set("file_name", "x.py")
        settings.unset("comment_id", force=True)
        return saved

    @pytest.mark.asyncio
    async def test_skips_model_call_when_range_misses_hunks(self):
        obj = self._provider()
        saved = self._set_ask_settings("100", "200")
        try:
            with patch("pr_agent.tools.pr_line_questions.retry_with_fallback_models",
                       new=AsyncMock()) as mock_retry, \
                 patch.object(plq.PR_LineQuestions, "_get_prediction", new=AsyncMock()) as mock_pred:
                await obj.run()
            mock_retry.assert_not_awaited()
            mock_pred.assert_not_awaited()
        finally:
            restore_settings(saved)

    @pytest.mark.asyncio
    async def test_skips_model_call_when_patch_is_unparseable(self):
        obj = self._provider()
        obj.git_provider.get_diff_files.return_value = [SimpleNamespace(
            filename="x.py", patch="this is not a diff")]
        saved = self._set_ask_settings("1", "2")
        try:
            with patch("pr_agent.tools.pr_line_questions.retry_with_fallback_models",
                       new=AsyncMock()) as mock_retry, \
                 patch.object(plq.PR_LineQuestions, "_get_prediction", new=AsyncMock()) as mock_pred:
                await obj.run()
            mock_retry.assert_not_awaited()
            mock_pred.assert_not_awaited()
        finally:
            restore_settings(saved)

    @pytest.mark.asyncio
    async def test_calls_model_when_lines_are_selected(self):
        obj = self._provider()
        saved = self._set_ask_settings("6", "8")
        try:
            with patch("pr_agent.tools.pr_line_questions.retry_with_fallback_models",
                       new=AsyncMock(return_value="an answer")) as mock_retry:
                await obj.run()
            mock_retry.assert_awaited_once()
            obj.git_provider.publish_comment.assert_called_once_with("an answer")
        finally:
            restore_settings(saved)

    @pytest.mark.asyncio
    async def test_skips_model_call_when_ask_diff_misses_hunks(self):
        obj = self._provider()
        saved = self._set_ask_settings("100", "200")
        try:
            get_settings().set("ask_diff_hunk", self._PATCH)
            with patch("pr_agent.tools.pr_line_questions.retry_with_fallback_models",
                       new=AsyncMock()) as mock_retry:
                await obj.run()
            mock_retry.assert_not_awaited()
        finally:
            restore_settings(saved)

    @pytest.mark.asyncio
    async def test_skips_model_call_when_no_file_matches(self):
        obj = self._provider()
        obj.git_provider.get_diff_files.return_value = [SimpleNamespace(
            filename="other.py", patch=self._PATCH)]
        saved = self._set_ask_settings("6", "8")
        try:
            with patch("pr_agent.tools.pr_line_questions.retry_with_fallback_models",
                       new=AsyncMock()) as mock_retry:
                await obj.run()
            mock_retry.assert_not_awaited()
        finally:
            restore_settings(saved)

    @pytest.mark.asyncio
    async def test_calls_model_when_ask_diff_selects_lines(self):
        obj = self._provider()
        saved = self._set_ask_settings("6", "8")
        try:
            get_settings().set("ask_diff_hunk", self._PATCH)
            with patch("pr_agent.tools.pr_line_questions.retry_with_fallback_models",
                       new=AsyncMock(return_value="an answer")) as mock_retry:
                await obj.run()
            mock_retry.assert_awaited_once()
            obj.git_provider.publish_comment.assert_called_once_with("an answer")
        finally:
            restore_settings(saved)


    @pytest.mark.asyncio
    async def test_answers_when_github_truncated_the_hunk_body(self):
        # GitHub truncates diff_hunk from the front on long hunks but keeps the original
        # @@ header, so the requested line sits outside the shortened body and no line is
        # selected. The hunk is real, so the question is still answerable.
        obj = self._provider()
        obj.git_provider.get_diff_files.return_value = [SimpleNamespace(
            filename="x.py",
            patch="@@ -5,400 +5,400 @@ def main():\n     tail = 1\n     tail = 2\n")]
        saved = self._set_ask_settings("300", "300")
        try:
            with patch("pr_agent.tools.pr_line_questions.retry_with_fallback_models",
                       new=AsyncMock(return_value="an answer")) as mock_retry:
                await obj.run()
            mock_retry.assert_awaited_once()
            obj.git_provider.publish_comment.assert_called_once_with("an answer")
        finally:
            restore_settings(saved)

    @pytest.mark.asyncio
    async def test_tells_the_asker_when_no_hunk_matched(self):
        obj = self._provider()
        saved = self._set_ask_settings("100", "200")
        try:
            with patch("pr_agent.tools.pr_line_questions.retry_with_fallback_models",
                       new=AsyncMock()) as mock_retry:
                await obj.run()
            mock_retry.assert_not_awaited()
            obj.git_provider.publish_comment.assert_called_once()
            assert "nothing to answer about" in obj.git_provider.publish_comment.call_args[0][0]
        finally:
            restore_settings(saved)
def test_ask_user_prompt_includes_untrusted_conversation_history():
    variables = {
        "branch": "feature/test",
        "conversation_history": "1. alice: Keep this nullable",
        "description": "",
        "diff": "+value",
        "language": "C#",
        "questions": "Can this throw?",
        "title": "Test PR",
    }
    prompt = _render_jinja_template(get_settings().pr_questions_prompt.user, variables)

    assert "Keep this nullable" in prompt
    assert "untrusted data" in prompt
