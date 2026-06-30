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


class TestRunLabelFailureIsolation:
    """``PRDescription.run`` must not abort when label refresh/publish fails.

    GitLabProvider.get_pr_labels(update=True) now raises on a refresh failure
    (PR #2484). Without isolation, that would bubble up to ``run``'s outer
    try/except and skip the description publish too. The labels block has an
    inline try/except so /describe degrades to "skip the label update".
    """

    def _instance_ready_for_publish(self, provider):
        """Build a PRDescription positioned at the publish step in ``run``.

        Bypasses __init__, the LLM call (_prepare_prediction), and the answer
        rendering (_prepare_pr_answer). Tests can mock the provider's
        get_pr_labels / publish_labels / publish_description behaviors.
        """
        with patch.object(PRDescription, "__init__", lambda self, *a, **kw: None):
            obj = PRDescription.__new__(PRDescription)
        obj.pr_id = "1"
        obj.prediction = "stub"  # truthy so the run() empty-prediction guard is skipped
        obj.user_description = ""
        obj.keys_fix = KEYS_FIX
        obj.vars = {}
        obj.data = {"type": ["Bug fix"]}
        obj.file_label_dict = {}
        obj.git_provider = provider
        # _prepare_prediction does nothing, _prepare_pr_answer returns a stub.
        obj._prepare_prediction = MagicMock(return_value=None)

        async def _async_noop(*_a, **_kw):
            return None

        # retry_with_fallback_models will be patched to call our _prepare_prediction stub
        obj._prepare_data = MagicMock(return_value=None)
        obj._prepare_file_labels = MagicMock(return_value={})
        obj._prepare_labels = MagicMock(return_value=["Bug fix"])
        obj._prepare_pr_answer = MagicMock(
            return_value=("AI title", "PR body", "", [])
        )
        obj._prepare_pr_answer_with_markers = MagicMock(
            return_value=("AI title", "PR body", "", [])
        )
        return obj, _async_noop

    @pytest.mark.asyncio
    async def test_run_publishes_description_when_label_refresh_raises(self):
        provider = MagicMock()
        provider.is_supported.return_value = True
        provider.get_pr_labels.side_effect = RuntimeError("transient gitlab error")
        provider.get_pr_url.return_value = "https://example.com/mr/1"

        obj, _async_noop = self._instance_ready_for_publish(provider)

        with patch(
            "pr_agent.tools.pr_description.get_settings"
        ) as mock_settings, patch(
            "pr_agent.tools.pr_description.retry_with_fallback_models", side_effect=_async_noop
        ), patch(
            "pr_agent.tools.pr_description.extract_and_cache_pr_tickets", side_effect=_async_noop
        ), patch(
            "pr_agent.tools.pr_description.get_user_labels", return_value=[]
        ):
            cfg = mock_settings.return_value
            cfg.config.publish_output = True
            cfg.config.get.side_effect = lambda key, default=None: {
                "is_auto_command": False,
                "output_relevant_configurations": False,
            }.get(key, default)
            cfg.pr_description.publish_labels = True
            cfg.pr_description.enable_semantic_files_types = False
            cfg.pr_description.use_description_markers = False
            cfg.pr_description.inline_file_summary = False
            cfg.pr_description.enable_help_text = False
            cfg.pr_description.enable_help_comment = False
            cfg.pr_description.publish_description_as_comment = False
            cfg.pr_description.generate_ai_title = True
            cfg.pr_description.final_update_message = False
            cfg.get.side_effect = lambda key, default=None: {
                "config": {"output_relevant_configurations": False},
            }.get(key, default)

            await obj.run()

        # The label step raised; the description publish must still happen.
        provider.publish_labels.assert_not_called()
        provider.publish_description.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_publishes_description_when_publish_labels_raises(self):
        provider = MagicMock()
        provider.is_supported.return_value = True
        provider.get_pr_labels.return_value = ["area/backend"]
        provider.publish_labels.side_effect = RuntimeError("transient gitlab error")
        provider.get_pr_url.return_value = "https://example.com/mr/1"

        obj, _async_noop = self._instance_ready_for_publish(provider)

        with patch(
            "pr_agent.tools.pr_description.get_settings"
        ) as mock_settings, patch(
            "pr_agent.tools.pr_description.retry_with_fallback_models", side_effect=_async_noop
        ), patch(
            "pr_agent.tools.pr_description.extract_and_cache_pr_tickets", side_effect=_async_noop
        ), patch(
            "pr_agent.tools.pr_description.get_user_labels", return_value=["area/backend"]
        ):
            cfg = mock_settings.return_value
            cfg.config.publish_output = True
            cfg.config.get.side_effect = lambda key, default=None: {
                "is_auto_command": False,
                "output_relevant_configurations": False,
            }.get(key, default)
            cfg.pr_description.publish_labels = True
            cfg.pr_description.enable_semantic_files_types = False
            cfg.pr_description.use_description_markers = False
            cfg.pr_description.inline_file_summary = False
            cfg.pr_description.enable_help_text = False
            cfg.pr_description.enable_help_comment = False
            cfg.pr_description.publish_description_as_comment = False
            cfg.pr_description.generate_ai_title = True
            cfg.pr_description.final_update_message = False
            cfg.get.side_effect = lambda key, default=None: {
                "config": {"output_relevant_configurations": False},
            }.get(key, default)

            await obj.run()

        provider.publish_labels.assert_called_once()
        provider.publish_description.assert_called_once()
