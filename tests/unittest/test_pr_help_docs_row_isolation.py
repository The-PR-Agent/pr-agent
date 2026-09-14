"""Regression tests for malformed model-produced /help_docs collections."""

from pr_agent.tools.pr_help_docs import (
    PRHelpDocs,
    format_markdown_q_and_a_response,
    get_valid_ranking_indices,
    modify_answer_section,
)


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
    tool.git_provider = type(
        "Provider",
        (),
        {"get_canonical_url_parts": lambda self, **kwargs: ("https://example.com/repo/blob/main", "")},
    )()

    assert tool._format_model_answer("The answer is here.", [{"file_name": "/docs/broken.md"}]) == (
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
