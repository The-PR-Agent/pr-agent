"""Handle malformed YAML without silently accepting unusable suggestion predictions."""
import pytest

from pr_agent.algo.pr_processing import FallbackEligibleError
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


def test_code_suggestions_retries_an_unparseable_prediction():
    """Malformed output must reach the configured fallback chain."""
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)

    with pytest.raises(FallbackEligibleError, match="Invalid code suggestions response"):
        tool._prepare_pr_code_suggestions(UNPARSEABLE)


def test_generate_labels_rejects_an_unparseable_prediction():
    """Let /generate_labels retry another model when its output cannot be parsed."""
    from pr_agent.tools.pr_generate_labels import PRGenerateLabels

    tool = PRGenerateLabels.__new__(PRGenerateLabels)
    tool.prediction = UNPARSEABLE

    with pytest.raises(ValueError):
        tool._prepare_data()


@pytest.mark.parametrize("payload", ["code_suggestions:\n", "code_suggestions: 5\n",
                                     "code_suggestions:\n  a: 1\n"])
def test_a_non_list_code_suggestions_value_is_rejected(payload):
    """Invalid suggestion containers are eligible for model fallback."""
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)

    with pytest.raises(FallbackEligibleError, match="Invalid code suggestions response"):
        tool._prepare_pr_code_suggestions(payload)


@pytest.mark.parametrize("payload", ["[]", "code_suggestions: []"])
def test_valid_empty_suggestions_do_not_trigger_fallback(payload):
    """An explicit empty list is a valid model result, not an error."""
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)

    assert tool._prepare_pr_code_suggestions(payload) == {"code_suggestions": []}
