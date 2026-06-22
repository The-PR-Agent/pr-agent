from unittest.mock import MagicMock, patch

import pytest
import yaml

from pr_agent.algo.types import FilePatchInfo
from pr_agent.tools.pr_description import PRDescription, sanitize_diagram

KEYS_FIX = ["filename:", "language:", "changes_summary:", "changes_title:", "description:", "title:"]

def _make_instance(prediction_yaml: str):
    """Create a PRDescription instance, bypassing __init__."""
    with patch.object(PRDescription, '__init__', lambda self, *a, **kw: None):
        obj = PRDescription.__new__(PRDescription)
    obj.prediction = prediction_yaml
    obj.keys_fix = KEYS_FIX
    obj.user_description = ""
    return obj


def _mock_settings():
    """Mock get_settings used by _prepare_data."""
    settings = MagicMock()
    settings.pr_description.add_original_user_description = False
    return settings


def _prediction_with_diagram(diagram_value: str) -> str:
    """Build a minimal YAML prediction string that includes changes_diagram."""
    return yaml.dump({
        'title': 'test',
        'description': 'test',
        'changes_diagram': diagram_value,
    })


class TestPRDescriptionDiagram:

    @patch('pr_agent.tools.pr_description.get_settings')
    def test_diagram_not_starting_with_fence_is_removed(self, mock_get_settings):
        mock_get_settings.return_value = _mock_settings()
        obj = _make_instance(_prediction_with_diagram('graph LR\nA --> B'))
        obj._prepare_data()
        assert 'changes_diagram' not in obj.data

    @patch('pr_agent.tools.pr_description.get_settings')
    def test_diagram_missing_closing_fence_is_appended(self, mock_get_settings):
        mock_get_settings.return_value = _mock_settings()
        obj = _make_instance(_prediction_with_diagram('```mermaid\ngraph LR\nA --> B'))
        obj._prepare_data()
        assert obj.data['changes_diagram'] == '\n```mermaid\ngraph LR\nA --> B\n```'

    @patch('pr_agent.tools.pr_description.get_settings')
    def test_backticks_inside_label_are_removed(self, mock_get_settings):
        mock_get_settings.return_value = _mock_settings()
        obj = _make_instance(_prediction_with_diagram('```mermaid\ngraph LR\nA["`file`"] --> B\n```'))
        obj._prepare_data()
        assert obj.data['changes_diagram'] == '\n```mermaid\ngraph LR\nA["file"] --> B\n```'

    @patch('pr_agent.tools.pr_description.get_settings')
    def test_backticks_outside_label_are_kept(self, mock_get_settings):
        mock_get_settings.return_value = _mock_settings()
        obj = _make_instance(_prediction_with_diagram('```mermaid\ngraph LR\nA["`file`"] -->|`edge`| B\n```'))
        obj._prepare_data()
        assert obj.data['changes_diagram'] == '\n```mermaid\ngraph LR\nA["file"] -->|`edge`| B\n```'

    @patch('pr_agent.tools.pr_description.get_settings')
    def test_normal_diagram_only_adds_newline(self, mock_get_settings):
        mock_get_settings.return_value = _mock_settings()
        obj = _make_instance(_prediction_with_diagram('```mermaid\ngraph LR\nA["file.py"] --> B["output"]\n```'))
        obj._prepare_data()
        assert obj.data['changes_diagram'] == '\n```mermaid\ngraph LR\nA["file.py"] --> B["output"]\n```'

    def test_none_input_returns_empty(self):
        assert sanitize_diagram(None) == ''

    def test_non_string_input_returns_empty(self):
        assert sanitize_diagram(123) == ''

    def test_non_mermaid_fence_returns_empty(self):
        assert sanitize_diagram('```python\nprint("hello")\n```') == ''


class TestPRDescriptionCore:
    def test_prepare_file_labels_groups_valid_files_and_skips_incomplete_entries(self):
        obj = _make_instance("")
        obj.pr_id = "1"
        obj.vars = {"include_file_summary_changes": True}
        obj.data = {
            "pr_files": [
                {
                    "filename": "src/app.py",
                    "changes_title": "Add cache",
                    "changes_summary": "Adds a bounded cache.",
                    "label": "backend",
                },
                {
                    "filename": "src/skip.py",
                    "changes_title": "Missing summary",
                    "label": "backend",
                },
                {
                    "filename": "docs/readme.md",
                    "changes_title": "Update docs",
                    "changes_summary": "Clarifies setup.",
                    "label": "docs",
                },
            ]
        }

        labels = obj._prepare_file_labels()

        assert labels == {
            "backend": [("src/app.py", "Add cache", "Adds a bounded cache.")],
            "docs": [("docs/readme.md", "Update docs", "Clarifies setup.")],
        }

    @patch('pr_agent.tools.pr_description.get_settings')
    def test_prepare_pr_answer_with_markers_replaces_plain_and_comment_markers(self, mock_get_settings):
        settings = MagicMock()
        settings.pr_description.generate_ai_title = True
        settings.pr_description.include_generated_by_header = False
        mock_get_settings.return_value = settings
        obj = _make_instance("")
        obj.pr_id = "1"
        obj.vars = {"title": "Original title"}
        obj.file_label_dict = {}
        obj.git_provider = MagicMock()
        obj.git_provider.last_commit_id.sha = "abc123"
        obj.user_description = (
            "pr_agent:type\n"
            "pr_agent:summary\n"
            "<!-- pr_agent:diagram -->\n"
        )
        obj.data = {
            "title": "AI title",
            "type": "Bug fix",
            "description": "Fixes the cache invalidation bug.",
            "changes_diagram": "\n```mermaid\ngraph LR\nA --> B\n```",
        }

        title, body, walkthrough, file_changes = obj._prepare_pr_answer_with_markers()

        assert title == "AI title"
        assert "Bug fix" in body
        assert "Fixes the cache invalidation bug." in body
        assert "```mermaid" in body
        assert walkthrough == ""
        assert file_changes == []

    @pytest.mark.asyncio
    async def test_extend_uncovered_files_adds_missing_diff_files_to_prediction(self):
        obj = _make_instance("")
        obj.pr_id = "1"
        obj.git_provider = MagicMock()
        obj.git_provider.get_diff_files.return_value = [
            FilePatchInfo("", "", "", "shown.py"),
            FilePatchInfo("", "", "", "missing.py"),
        ]
        prediction = """
pr_files:
  - filename: shown.py
    changes_title: Existing summary
    label: backend
"""

        extended = await obj.extend_uncovered_files(prediction)
        loaded = yaml.safe_load(extended)

        assert [file["filename"].strip() for file in loaded["pr_files"]] == ["shown.py", "missing.py"]
        assert loaded["pr_files"][1]["label"].strip() == "additional files"
