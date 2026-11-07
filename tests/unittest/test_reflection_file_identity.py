from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


def test_reordered_cross_file_feedback_is_not_assigned_by_position():
    tool = object.__new__(PRCodeSuggestions)
    suggestions = [
        {"relevant_file": "first.py", "relevant_lines_start": 1},
        {"relevant_file": "second.py", "relevant_lines_start": 1},
    ]
    response = {"code_suggestions": [
        {"relevant_file": "second.py", "suggestion_score": 10, "why": "Second file assessment"},
        {"relevant_file": "first.py", "suggestion_score": 0, "why": "First file assessment"},
    ]}
    assert tool._has_usable_reflection_feedback(response, suggestions) is False
    assert response["code_suggestions"] == [None, None]


def test_correct_feedback_survives_a_cross_file_sibling_mismatch():
    tool = object.__new__(PRCodeSuggestions)
    first = {"relevant_file": "first.py", "suggestion_score": 8, "why": "Valid assessment"}
    response = {"code_suggestions": [
        first, {"relevant_file": "wrong.py", "suggestion_score": 10, "why": "Wrong file"},
    ]}
    suggestions = [
        {"relevant_file": "first.py", "relevant_lines_start": 1},
        {"relevant_file": "second.py", "relevant_lines_start": 1},
    ]
    assert tool._has_usable_reflection_feedback(response, suggestions) is True
    assert response["code_suggestions"] == [first, None]
