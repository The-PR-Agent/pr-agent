import pytest

from pr_agent.commands.pr_description import PRDescription


@pytest.mark.parametrize("summary", [42, {"unexpected": "mapping"}, ["unexpected list"]])
def test_optional_malformed_summary_does_not_hide_a_valid_file(summary):
    command = object.__new__(PRDescription)
    command.pr_id = "synthetic-pr"
    command.vars = {"include_file_summary_changes": False}
    command.data = {"pr_files": [{
        "filename": "app.py", "changes_title": "Handle missing input",
        "label": "Enhancement", "changes_summary": summary,
    }]}
    assert command._prepare_file_labels() == {"enhancement": [("app.py", "Handle missing input", "")]}


def test_required_malformed_summary_is_still_rejected():
    command = object.__new__(PRDescription)
    command.pr_id = "synthetic-pr"
    command.vars = {"include_file_summary_changes": True}
    command.data = {"pr_files": [{
        "filename": "app.py", "changes_title": "Handle missing input",
        "label": "Enhancement", "changes_summary": {"unexpected": "mapping"},
    }]}
    assert command._prepare_file_labels() == {}
