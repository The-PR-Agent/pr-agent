"""Read the labels list whatever entry shape the model returns."""
import pytest

from pr_agent.tools.pr_generate_labels import PRGenerateLabels


def labels(prediction, variables=None):
    tool = PRGenerateLabels.__new__(PRGenerateLabels)
    tool.prediction = prediction
    tool.variables = variables or {"title": "a title"}
    tool.pr_id = "repo#1"
    tool._prepare_data()
    return tool._prepare_labels()


def test_read_the_documented_list_of_strings():
    """Keep the documented shape working exactly as before."""
    assert labels("labels:\n- bug fix\n- tests\n") == ["bug fix", "tests"]


def test_read_a_comma_separated_string():
    """Keep the string form the parser already accepts."""
    assert labels("labels: |\n  bug fix, tests\n") == ["bug fix", "tests"]


@pytest.mark.parametrize("prediction, expected", [
    ("labels:\n- name: bug fix\n", ["bug fix"]),
    ("labels:\n- label: bug fix\n", ["bug fix"]),
    ("labels:\n- 1\n", ["1"]),
])
def test_read_an_entry_that_is_not_a_plain_string(prediction, expected):
    """A mapping or a number must not fail the whole /generate_labels run."""
    assert labels(prediction) == expected


def test_drop_an_entry_that_carries_no_name():
    """An unusable entry is skipped, not turned into an empty label."""
    assert labels("labels:\n- {}\n- bug fix\n") == ["bug fix"]


def test_no_labels_key_returns_nothing():
    """Keep returning an empty list when the model produced no labels."""
    assert labels("types:\n- bug fix\n") == []
