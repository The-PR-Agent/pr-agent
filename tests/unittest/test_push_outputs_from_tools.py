"""`/describe` and `/improve` reach the configured sinks, the same way `/review` already does.

`push_outputs` was wired into `PRReviewer` only, so an operator running the three default
automatic commands received one of the three results. The two questions worth pinning down are
whether each tool emits at all, and what it sends: a sink is not a git provider, so the GFM
table `/improve` publishes to GitHub is not what should arrive in Slack.
"""
from unittest.mock import MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions, render_suggestions_markdown
from pr_agent.tools.pr_description import PRDescription
from pr_agent.tools.pr_reviewer import PRReviewer

SUGGESTION = {
    "relevant_file": "src/foo.py",
    "relevant_lines_start": 12,
    "relevant_lines_end": 18,
    "label": "possible issue",
    "score": 8,
    "one_sentence_summary": "Guard the index before dereferencing it",
    "suggestion_content": "The loop can run past the end of the list.",
}


# --------------------------------------------------------------------------------------
# Which tools emit
# --------------------------------------------------------------------------------------
def test_review_still_emits():
    """Control: the tool that already emitted keeps doing so."""
    assert "push_outputs" in PRReviewer._prepare_pr_review.__code__.co_names


def test_describe_emits_from_its_publish_path():
    assert "push_outputs" in PRDescription.run.__code__.co_names


def test_improve_emits_from_its_publish_path():
    assert "push_outputs" in PRCodeSuggestions.run.__code__.co_names


# --------------------------------------------------------------------------------------
# What `/improve` sends
#
# The provider gets `generate_summarized_suggestions`, a GFM table wrapped in <table> and
# <details> HTML, and only when the provider supports gfm_markdown. Slack and Telegram render
# neither, so sending that - or, worse, no markdown at all, leaving the sink with raw JSON -
# makes the notification unreadable.
# --------------------------------------------------------------------------------------
def test_the_suggestion_markdown_names_the_location():
    rendered = render_suggestions_markdown({"code_suggestions": [SUGGESTION]})

    assert "**src/foo.py:12-18**" in rendered


def test_the_suggestion_markdown_carries_the_label_and_score():
    rendered = render_suggestions_markdown({"code_suggestions": [SUGGESTION]})

    assert "possible issue" in rendered
    assert "score 8" in rendered


def test_the_suggestion_markdown_carries_the_summary():
    rendered = render_suggestions_markdown({"code_suggestions": [SUGGESTION]})

    assert "Guard the index before dereferencing it" in rendered


def test_the_suggestion_markdown_is_not_html():
    """The point of a separate renderer: no <table>/<details> that a chat client shows raw."""
    rendered = render_suggestions_markdown({"code_suggestions": [SUGGESTION]})

    assert "<table>" not in rendered
    assert "<details>" not in rendered


def test_a_single_line_suggestion_is_not_written_as_a_range():
    rendered = render_suggestions_markdown(
        {"code_suggestions": [{**SUGGESTION, "relevant_lines_start": 12, "relevant_lines_end": 12}]})

    assert "**src/foo.py:12**" in rendered


def test_every_suggestion_is_rendered():
    rendered = render_suggestions_markdown({"code_suggestions": [
        SUGGESTION,
        {**SUGGESTION, "relevant_file": "src/bar.py", "one_sentence_summary": "Close the file"},
    ]})

    assert "src/foo.py" in rendered
    assert "src/bar.py" in rendered
    assert "Close the file" in rendered


def test_the_content_is_used_when_there_is_no_one_sentence_summary():
    rendered = render_suggestions_markdown(
        {"code_suggestions": [{k: v for k, v in SUGGESTION.items() if k != "one_sentence_summary"}]})

    assert "The loop can run past the end of the list." in rendered


@pytest.mark.parametrize("data", [
    {},
    {"code_suggestions": []},
    {"code_suggestions": None},
    {"code_suggestions": ["not a dict"]},
])
def test_an_empty_or_malformed_answer_still_renders(data):
    """The renderer runs on the publish path, so it must not be able to fail the command."""
    assert render_suggestions_markdown(data) == "## PR Code Suggestions\n\nNo suggestions to report."


def test_a_suggestion_without_a_file_is_still_rendered():
    rendered = render_suggestions_markdown(
        {"code_suggestions": [{"one_sentence_summary": "Something", "relevant_file": ""}]})

    assert "(file not reported)" in rendered
    assert "Something" in rendered


# --------------------------------------------------------------------------------------
# End to end through the real publish paths
# --------------------------------------------------------------------------------------
@pytest.fixture
def emitted(monkeypatch):
    calls = []
    monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.push_outputs",
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr("pr_agent.tools.pr_description.push_outputs",
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(get_settings().config, "publish_output", True)
    return calls


async def test_improve_sends_readable_markdown(emitted, monkeypatch):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = MagicMock()
    tool.git_provider.get_files.return_value = ["src/foo.py"]
    tool.git_provider.is_supported.return_value = False
    tool.pr_url = "https://github.com/org/repo/pull/1"
    tool.progress_response = None
    tool._output_published = False
    tool.is_extended = False
    monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.retry_with_fallback_models",
                        MagicMock(return_value=_awaitable({"code_suggestions": [SUGGESTION]})))

    await tool.run()

    assert emitted, "the improve publish path emitted nothing"
    _args, kwargs = emitted[0]
    assert kwargs["markdown"] is not None
    assert "src/foo.py:12-18" in kwargs["markdown"]
    assert "<table>" not in kwargs["markdown"]


def _awaitable(value):
    async def _coro(*args, **kwargs):
        return value
    return _coro()


async def test_improve_still_publishes_to_the_provider(emitted, monkeypatch):
    """Control: emitting to a sink is additional, not instead of the pull request comment."""
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = MagicMock()
    tool.git_provider.get_files.return_value = ["src/foo.py"]
    tool.git_provider.is_supported.return_value = False
    tool.pr_url = "https://github.com/org/repo/pull/1"
    tool.progress_response = None
    tool._output_published = False
    tool.is_extended = False
    monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.retry_with_fallback_models",
                        MagicMock(return_value=_awaitable({"code_suggestions": [SUGGESTION]})))

    await tool.run()

    tool.git_provider.remove_initial_comment.assert_called_once()
