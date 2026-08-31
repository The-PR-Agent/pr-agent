"""Parse an /improve response that does not carry the documented code_suggestions list."""
import pytest

from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

DOCUMENTED = """code_suggestions:
- relevant_file: |
    src/app.py
  suggestion_content: |
    do x
  existing_code: |
    a = 1
  improved_code: |
    a = 2
  one_sentence_summary: |
    fix a
  relevant_lines_start: 1
  relevant_lines_end: 1
  label: |
    best practice
"""


def parse(prediction):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    return tool._prepare_pr_code_suggestions(prediction)


def test_parse_the_documented_response():
    """Keep the documented response parsing exactly as before."""
    assert len(parse(DOCUMENTED)["code_suggestions"]) == 1


@pytest.mark.parametrize("prediction, reason", [
    ("No suggestions found for this PR.\n", "the model answered in prose"),
    ("suggestions:\n- relevant_file: src/app.py\n", "the model renamed the key"),
    ("code_suggestions:\n", "the model emitted the key with no value"),
    ("code_suggestions: |\n  none\n", "the model wrote a string instead of a list"),
    ("::: not : valid : yaml :::\n\t- [", "the response could not be parsed at all"),
])
def test_return_an_empty_suggestion_list(prediction, reason):
    """Report no suggestions instead of failing the whole /improve run."""
    assert parse(prediction) == {"code_suggestions": []}, reason


def test_a_bare_list_response_is_still_wrapped():
    """Keep accepting a top-level list, which the parser already normalises."""
    prediction = ("- relevant_file: |\n    src/app.py\n  suggestion_content: |\n    do x\n"
                  "  existing_code: |\n    a = 1\n  improved_code: |\n    a = 2\n"
                  "  one_sentence_summary: |\n    fix a\n  label: |\n    best practice\n")

    assert len(parse(prediction)["code_suggestions"]) == 1
