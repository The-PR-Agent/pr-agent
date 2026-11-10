import pytest
import yaml

from pr_agent.commands.pr_code_suggestions import PRCodeSuggestions
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


@pytest.mark.parametrize("limit", [5, "5"])
def test_quoted_suggestion_length_caps_do_not_discard_valid_suggestions(limit):
    snapshot = snapshot_settings(("pr_code_suggestions.max_code_suggestion_length",))
    get_settings().set("pr_code_suggestions.max_code_suggestion_length", limit)
    suggestion = {
        "relevant_file": "app.py", "label": "possible bug", "one_sentence_summary": "Handle the result",
        "suggestion_content": "Check the result before returning it.",
        "existing_code": "old()", "improved_code": "replacement_call()",
    }
    try:
        data = object.__new__(PRCodeSuggestions)._prepare_pr_code_suggestions(
            yaml.safe_dump({"code_suggestions": [suggestion]})
        )
    finally:
        restore_settings(snapshot)
    assert len(data["code_suggestions"]) == 1
    assert data["code_suggestions"][0]["improved_code"].startswith("repla\n")
    assert data["code_suggestions"][0]["_is_truncated"] is True


@pytest.mark.parametrize("limit", ["not a number", None, []])
def test_unusable_length_cap_does_not_erase_valid_suggestions(limit):
    snapshot = snapshot_settings(("pr_code_suggestions.max_code_suggestion_length",))
    get_settings().set("pr_code_suggestions.max_code_suggestion_length", limit)
    suggestion = {
        "relevant_file": "app.py", "label": "possible bug", "one_sentence_summary": "Handle the result",
        "suggestion_content": "Check the result before returning it.",
        "existing_code": "old()", "improved_code": "replacement_call()",
    }
    try:
        data = object.__new__(PRCodeSuggestions)._prepare_pr_code_suggestions(
            yaml.safe_dump({"code_suggestions": [suggestion]})
        )
    finally:
        restore_settings(snapshot)
    assert len(data["code_suggestions"]) == 1
    assert data["code_suggestions"][0]["improved_code"] == "replacement_call()"
