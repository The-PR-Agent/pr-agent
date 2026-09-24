"""Regression tests for malformed model-produced /help_docs collections."""

import pytest

from pr_agent.tools.pr_help_docs import (
    PRHelpDocs,
    format_markdown_q_and_a_response,
    get_valid_ranking_indices,
    modify_answer_section,
)


class StubProvider:
    def get_canonical_url_parts(self, **kwargs):
        return "https://example.com/repo/blob/main", ""


def test_format_markdown_q_and_a_response_preserves_valid_sources_when_one_row_is_malformed():
    answer = format_markdown_q_and_a_response(
        "Where is the guide?",
        "The answer is here.",
        [
            {"file_name": "/docs/guide.md", "relevant_section_header_string": "Getting started"},
            {"file_name": "/docs/broken.md"},
        ],
        [".md"],
        "https://example.com/repo/blob/main",
    )

    assert "The answer is here." in answer
    assert "https://example.com/repo/blob/main/docs/guide.md#getting-started" in answer
    assert "broken.md" not in answer


def test_format_markdown_q_and_a_response_omits_sources_when_all_rows_are_malformed():
    answer = format_markdown_q_and_a_response(
        "Where is the guide?",
        "The answer is here.",
        [{"file_name": "/docs/broken.md"}, None, {"relevant_section_header_string": "Missing file"}],
        [".md"],
        "https://example.com/repo/blob/main",
    )

    assert answer == "### Question: \nWhere is the guide?\n\n### Answer:\nThe answer is here.\n\n"
    assert "Relevant Sources" not in answer


def test_format_markdown_q_and_a_response_omits_malformed_source_collection():
    answer = format_markdown_q_and_a_response(
        "Where is the guide?",
        "The answer is here.",
        {"file_name": "/docs/guide.md", "relevant_section_header_string": "Getting started"},
        [".md"],
        "https://example.com/repo/blob/main",
    )

    assert answer == "### Question: \nWhere is the guide?\n\n### Answer:\nThe answer is here.\n\n"


def test_format_markdown_q_and_a_response_routes_unexpected_formatter_errors_to_failure_handler(monkeypatch):
    def fail_to_format_header(header):
        raise RuntimeError("unexpected formatter failure")

    monkeypatch.setattr("pr_agent.tools.pr_help_docs.format_markdown_header", fail_to_format_header)

    answer = format_markdown_q_and_a_response(
        "Where is the guide?",
        "The answer is here.",
        [{"file_name": "/docs/guide.md", "relevant_section_header_string": "Getting started"}],
        [".md"],
        "https://example.com/repo/blob/main",
    )

    assert answer == ""


def test_modify_answer_section_preserves_answer_without_sources():
    assert modify_answer_section("### Answer:\nThe answer is here.\n\n") == (
        "### :bulb: Auto-generated documentation-based answer:\nThe answer is here.\n\n"
    )


def test_format_model_answer_preserves_answer_when_no_sources_survive():
    tool = PRHelpDocs.__new__(PRHelpDocs)
    tool.question = "Where is the guide?"
    tool.supported_doc_exts = [".md"]
    tool.return_as_string = True
    tool.repo_url = "https://example.com/org/repo"
    tool.repo_url_given_explicitly = True
    tool.repo_desired_branch = "main"
    tool.git_provider = StubProvider()

    assert tool._format_model_answer("The answer is here.", [{"file_name": "/docs/broken.md"}]) == (
        "### :bulb: Auto-generated documentation-based answer:\nThe answer is here.\n\n"
    )


@pytest.mark.asyncio
async def test_run_preserves_answer_when_model_returns_empty_source_list(monkeypatch):
    async def fake_retry_with_fallback_models(*args, **kwargs):
        return """question_is_relevant: 1\nresponse: The answer is here.\nrelevant_sections: []\n"""

    monkeypatch.setattr("pr_agent.tools.pr_help_docs.retry_with_fallback_models", fake_retry_with_fallback_models)

    tool = PRHelpDocs.__new__(PRHelpDocs)
    tool.question = "Where is the guide?"
    tool.return_as_string = True
    tool.repo_url = "https://example.com/org/repo"
    tool.repo_url_given_explicitly = True
    tool.repo_desired_branch = "main"
    tool.supported_doc_exts = [".md"]
    tool.git_provider = StubProvider()
    tool.ai_handler = object()
    tool.vars = {"question": tool.question, "snippets": ""}
    tool._gen_filenames_to_contents_map_from_repo = lambda: {"/docs/guide.md": "# Guide\n\nGuide content."}
    tool._trim_docs_input = lambda docs_input, *args, only_return_if_trim_needed=False, **kwargs: (
        False if only_return_if_trim_needed else docs_input
    )

    assert await tool.run() == (
        "### :bulb: Auto-generated documentation-based answer:\nThe answer is here.\n\n"
    )


def test_get_valid_ranking_indices_preserves_valid_order_and_skips_malformed_rows():
    assert get_valid_ranking_indices(
        [
            {"idx": "2"},
            {"idx": True},
            {"idx": "not-a-number"},
            {"idx": 0},
            {"idx": 2},
            {"idx": 4},
            {"idx": 1.0},
            "not-a-row",
        ],
        3,
    ) == [2, 0, 2]


def test_get_valid_ranking_indices_skips_non_list_collections():
    assert get_valid_ranking_indices({"idx": 0}, 1) == []
    assert get_valid_ranking_indices(True, 1) == []


def test_get_valid_ranking_indices_skips_all_malformed_rows():
    assert get_valid_ranking_indices(
        [{"idx": "not-a-number"}, {"idx": -1}, {"idx": 2}, None],
        2,
    ) == []


@pytest.mark.asyncio
async def test_rank_docs_preserves_valid_rows_when_model_ranking_is_partially_malformed(monkeypatch):
    async def fake_retry_with_fallback_models(*args, **kwargs):
        return """relevant_files_ranking:\n  - idx: 2\n  - invalid: row\n  - idx: 0\n"""

    monkeypatch.setattr("pr_agent.tools.pr_help_docs.retry_with_fallback_models", fake_retry_with_fallback_models)

    tool = PRHelpDocs.__new__(PRHelpDocs)
    tool.ai_handler = object()
    tool.vars = {"question": "Where is the guide?", "snippets": ""}
    tool._trim_docs_input = lambda docs_input, *args, **kwargs: docs_input
    docs = {
        "/docs/first.md": "# First\n\nFirst document.",
        "/docs/second.md": "# Second\n\nSecond document.",
        "/docs/third.md": "# Third\n\nThird document.",
    }

    ranked_prompt = await tool._rank_docs_and_return_them_as_prompt(docs, 10_000)

    assert "/docs/second.md" not in ranked_prompt
    assert ranked_prompt.index("/docs/third.md") < ranked_prompt.index("/docs/first.md")
