import pytest

from pr_agent.commands.pr_code_suggestions import PRCodeSuggestions


def test_reordered_cross_file_feedback_is_not_assigned_by_position():
    command = object.__new__(PRCodeSuggestions)
    suggestions = [
        {"relevant_file": "first.py", "relevant_lines_start": 1},
        {"relevant_file": "second.py", "relevant_lines_start": 1},
    ]
    response = {"code_suggestions": [
        {"relevant_file": "second.py", "suggestion_score": 10, "why": "Second file assessment"},
        {"relevant_file": "first.py", "suggestion_score": 0, "why": "First file assessment"},
    ]}
    assert command._has_usable_reflection_feedback(response, suggestions) is False
    assert response["code_suggestions"] == [None, None]


def test_correct_feedback_survives_a_cross_file_sibling_mismatch():
    command = object.__new__(PRCodeSuggestions)
    first = {"relevant_file": "first.py", "suggestion_score": 8, "why": "Valid assessment"}
    response = {"code_suggestions": [
        first, {"relevant_file": "wrong.py", "suggestion_score": 10, "why": "Wrong file"},
    ]}
    suggestions = [
        {"relevant_file": "first.py", "relevant_lines_start": 1},
        {"relevant_file": "second.py", "relevant_lines_start": 1},
    ]
    assert command._has_usable_reflection_feedback(response, suggestions) is True
    assert response["code_suggestions"] == [first, None]


@pytest.mark.parametrize("reported", ["`src/a.py`", "./src/a.py", "/src/a.py"])
def test_decorated_file_paths_keep_feedback(reported):
    command = object.__new__(PRCodeSuggestions)
    entry = {"relevant_file": reported, "suggestion_score": 8, "why": "Valid assessment"}
    response = {"code_suggestions": [entry]}
    suggestions = [{"relevant_file": "src/a.py", "relevant_lines_start": 1}]
    assert command._has_usable_reflection_feedback(response, suggestions) is True
    assert response["code_suggestions"] == [entry]
