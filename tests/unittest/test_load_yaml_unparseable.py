"""An unparseable AI prediction must degrade, not crash the tool that asked for it."""
from pr_agent.algo.utils import load_yaml

UNPARSEABLE = "::: not : valid : yaml :::\n\t- ["


def test_return_an_empty_mapping_for_an_unparseable_prediction():
    """Return an empty mapping so callers can use `in` and `[]` without a None check."""
    assert load_yaml(UNPARSEABLE) == {}


def test_a_parseable_prediction_is_unchanged():
    """Keep returning the parsed document for valid input."""
    assert load_yaml("review:\n  score: 8") == {"review": {"score": 8}}


def test_membership_test_does_not_raise():
    """`'review' not in data` is what pr_reviewer does first; it must not raise."""
    data = load_yaml(UNPARSEABLE)
    assert "review" not in data


def test_get_does_not_raise():
    """`.get(...)` is what pr_description and pr_help_message do first."""
    assert load_yaml(UNPARSEABLE).get("pr_files", []) == []


def test_reviewer_reports_the_parse_failure_instead_of_crashing():
    """pr_reviewer's own 'Failed to parse review data' path becomes reachable."""
    from pr_agent.tools.pr_reviewer import PRReviewer

    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.prediction = UNPARSEABLE

    assert reviewer._prepare_pr_review() == ""


def test_code_suggestions_returns_an_empty_list_instead_of_crashing():
    """pr_code_suggestions subscripts the result, so it needs its own guard."""
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)

    assert tool._prepare_pr_code_suggestions(UNPARSEABLE) == {"code_suggestions": []}


def test_generate_labels_membership_check_does_not_raise():
    """pr_generate_labels opens _prepare_labels with `'labels' in self.data`, which used to
    raise TypeError on a None result."""
    from pr_agent.tools.pr_generate_labels import PRGenerateLabels

    tool = PRGenerateLabels.__new__(PRGenerateLabels)
    tool.prediction = UNPARSEABLE
    tool._prepare_data()

    assert tool.data == {}
    assert "labels" not in tool.data
