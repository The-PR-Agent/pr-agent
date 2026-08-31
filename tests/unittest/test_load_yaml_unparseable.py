"""Degrade gracefully when an AI prediction cannot be parsed as YAML."""
import pytest

from pr_agent.algo.utils import load_yaml

UNPARSEABLE = "::: not : valid : yaml :::\n\t- ["


def test_return_an_empty_mapping_for_an_unparseable_prediction():
    """Return an empty mapping so callers can use `in` and `[]` without a None check."""
    assert load_yaml(UNPARSEABLE) == {}


def test_a_parseable_prediction_is_unchanged():
    """Keep returning the parsed document for valid input."""
    assert load_yaml("review:\n  score: 8") == {"review": {"score": 8}}


def test_membership_test_does_not_raise():
    """Support `'review' not in data`, which is the first thing pr_reviewer does."""
    data = load_yaml(UNPARSEABLE)
    assert "review" not in data


def test_get_does_not_raise():
    """Support `.get(...)`, which is the first thing pr_description and pr_help_message do."""
    assert load_yaml(UNPARSEABLE).get("pr_files", []) == []


def test_reviewer_reports_the_parse_failure_instead_of_crashing():
    """Reach pr_reviewer's own 'Failed to parse review data' path."""
    from pr_agent.tools.pr_reviewer import PRReviewer

    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.prediction = UNPARSEABLE

    assert reviewer._prepare_pr_review() == ""


def test_code_suggestions_returns_an_empty_list_instead_of_crashing():
    """Guard pr_code_suggestions, which subscripts the parsed result."""
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)

    assert tool._prepare_pr_code_suggestions(UNPARSEABLE) == {"code_suggestions": []}


def test_generate_labels_membership_check_does_not_raise():
    """Support `'labels' in self.data`, which pr_generate_labels does before anything else."""
    from pr_agent.tools.pr_generate_labels import PRGenerateLabels

    tool = PRGenerateLabels.__new__(PRGenerateLabels)
    tool.prediction = UNPARSEABLE
    tool._prepare_data()

    assert tool.data == {}
    assert "labels" not in tool.data


@pytest.mark.parametrize("payload", ["code_suggestions:\n", "code_suggestions: 5\n",
                                     "code_suggestions:\n  a: 1\n"])
def test_a_non_list_code_suggestions_value_is_rejected(payload):
    """Return an empty result when code_suggestions is present but not a list."""
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)

    assert tool._prepare_pr_code_suggestions(payload) == {"code_suggestions": []}


def test_an_unparseable_chunk_is_recorded_so_the_coverage_footer_still_reports_it():
    """The empty result must not read as a successful chunk (#2867 counts failed chunks)."""
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)

    assert tool._prepare_pr_code_suggestions(UNPARSEABLE) == {"code_suggestions": []}
    assert tool.parse_failure_count == 1

    tool.failed_chunk_count = tool.parse_failure_count
    tool.total_chunk_count = 2
    assert "1 of 2 analysis chunks failed" in tool._get_suggestions_coverage_footer()
