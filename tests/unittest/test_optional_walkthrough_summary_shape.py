import pytest

from pr_agent.commands.pr_description import PRDescription


@pytest.mark.parametrize("summary", [42, {"unexpected": "mapping"}, ["unexpected list"]])
def test_optional_malformed_summary_does_not_hide_a_valid_file(summary):
    tool = object.__new__(PRDescription)
    tool.pr_id = "synthetic-pr"
    tool.vars = {"include_file_summary_changes": False}
    tool.data = {"pr_files": [{
        "filename": "app.py", "changes_title": "Handle missing input",
        "label": "Enhancement", "changes_summary": summary,
    }]}
    assert tool._prepare_file_labels() == {"enhancement": [("app.py", "Handle missing input", "")]}


def test_required_malformed_summary_is_still_rejected():
    tool = object.__new__(PRDescription)
    tool.pr_id = "synthetic-pr"
    tool.vars = {"include_file_summary_changes": True}
    tool.data = {"pr_files": [{
        "filename": "app.py", "changes_title": "Handle missing input",
        "label": "Enhancement", "changes_summary": {"unexpected": "mapping"},
    }]}
    assert tool._prepare_file_labels() == {}
