from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.run_metadata import init_run_metadata, record_ai_call, record_model_used
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from pr_agent.tools.pr_description import PRDescription
from pr_agent.tools.pr_reviewer import PRReviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_KEYS_REVIEW = (
    "config.output_run_metadata",
    "config.publish_output",
    "pr_reviewer.enable_help_text",
)
_TRACKED_KEYS_DESCRIPTION = (
    "config.output_run_metadata",
    "config.publish_output",
    "config.is_auto_command",
    "data",
    "pr_description.enable_semantic_files_types",
    "pr_description.publish_labels",
    "pr_description.use_description_markers",
    "pr_description.enable_help_text",
    "pr_description.enable_help_comment",
)
_TRACKED_KEYS_SUGGESTIONS = (
    "config.output_run_metadata",
    "config.publish_output",
    "config.publish_output_progress",
    "config.is_auto_command",
    "pr_code_suggestions.commitable_code_suggestions",
    "pr_code_suggestions.demand_code_suggestions_self_review",
    "pr_code_suggestions.enable_chat_text",
    "pr_code_suggestions.enable_help_text",
    "pr_code_suggestions.persistent_comment",
    "pr_code_suggestions.dual_publishing_score_threshold",
)


@pytest.fixture(autouse=True)
def isolate_run_metadata():
    """Restore the run-metadata ContextVar after each test."""
    from pr_agent.algo import run_metadata

    token = run_metadata._run_metadata.set(run_metadata._run_metadata.get())
    yield
    run_metadata._run_metadata.reset(token)


class _Usage:
    def __init__(self, prompt_tokens, completion_tokens, total_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


def _seed_run_metadata():
    init_run_metadata()
    record_model_used("openai/gpt-5.4", is_fallback=False)
    record_ai_call(_Usage(10, 2, 12))


def _seeded_init_run_metadata():
    _seed_run_metadata()
    return None


async def _noop_async(*_args, **_kwargs):
    return None


def test_flag_defaults_to_false():
    assert get_settings().config.get("output_run_metadata", None) is False


def test_pr_reviewer_appends_run_metadata_only_when_enabled():
    snapshot = snapshot_settings(_TRACKED_KEYS_REVIEW)
    try:
        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.prediction = """
review:
  estimated_effort_to_review_[1-5]: "2"
  security_concerns: "No"
"""
        reviewer.incremental = SimpleNamespace(is_incremental=False)
        reviewer.git_provider = MagicMock()
        reviewer.git_provider.is_supported.side_effect = lambda cap: cap == "gfm_markdown"
        reviewer.git_provider.get_diff_files.return_value = []

        _seed_run_metadata()
        get_settings().set("config.publish_output", False)
        get_settings().set("config.output_run_metadata", False)
        get_settings().pr_reviewer.enable_help_text = False
        without_metadata = reviewer._prepare_pr_review()

        _seed_run_metadata()
        get_settings().set("config.output_run_metadata", True)
        with_metadata = reviewer._prepare_pr_review()

        assert "🔎 PR-Agent run metadata" not in without_metadata
        assert "🔎 PR-Agent run metadata" in with_metadata
    finally:
        restore_settings(snapshot)


@pytest.mark.asyncio
async def test_pr_description_appends_run_metadata_only_when_enabled(monkeypatch):
    snapshot = snapshot_settings(_TRACKED_KEYS_DESCRIPTION)
    try:
        description = PRDescription.__new__(PRDescription)
        description.pr_id = "1"
        description.vars = {}
        description.prediction = "prediction"
        description.file_label_dict = {}
        description.git_provider = MagicMock()
        description.git_provider.is_supported.side_effect = lambda cap: cap == "gfm_markdown"

        description._prepare_data = MagicMock()
        description._prepare_pr_answer = MagicMock(return_value=("AI title", "Base description body", "", []))

        monkeypatch.setattr("pr_agent.tools.pr_description.init_run_metadata", _seeded_init_run_metadata)
        monkeypatch.setattr("pr_agent.tools.pr_description.extract_and_cache_pr_tickets", _noop_async)
        monkeypatch.setattr("pr_agent.tools.pr_description.retry_with_fallback_models", _noop_async)

        get_settings().set("config.publish_output", False)
        get_settings().set("config.is_auto_command", False)
        get_settings().pr_description.enable_semantic_files_types = False
        get_settings().pr_description.publish_labels = False
        get_settings().pr_description.use_description_markers = False
        get_settings().pr_description.enable_help_text = False
        get_settings().pr_description.enable_help_comment = False

        get_settings().set("config.output_run_metadata", False)
        await description.run()
        without_metadata = get_settings().data["artifact"]

        get_settings().set("config.output_run_metadata", True)
        await description.run()
        with_metadata = get_settings().data["artifact"]

        assert "🔎 PR-Agent run metadata" not in without_metadata
        assert "🔎 PR-Agent run metadata" in with_metadata
    finally:
        restore_settings(snapshot)


@pytest.mark.asyncio
async def test_pr_code_suggestions_appends_run_metadata_only_when_enabled(monkeypatch):
    snapshot = snapshot_settings(_TRACKED_KEYS_SUGGESTIONS)
    try:
        suggestions = PRCodeSuggestions.__new__(PRCodeSuggestions)
        suggestions.pr_url = "https://example/pr/1"
        suggestions.progress = "progress"
        suggestions.progress_response = None
        suggestions.git_provider = MagicMock()
        suggestions.git_provider.get_files.return_value = ["changed.py"]
        suggestions.git_provider.is_supported.side_effect = lambda cap: cap == "gfm_markdown"
        suggestions.generate_summarized_suggestions = MagicMock(return_value="Base suggestions body")

        async def _fake_retry(*_args, **_kwargs):
            return {"code_suggestions": [{"label": "style"}]}

        monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.init_run_metadata", _seeded_init_run_metadata)
        monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.retry_with_fallback_models", _fake_retry)

        get_settings().set("config.publish_output", True)
        get_settings().set("config.publish_output_progress", False)
        get_settings().set("config.is_auto_command", False)
        get_settings().pr_code_suggestions.commitable_code_suggestions = False
        get_settings().pr_code_suggestions.demand_code_suggestions_self_review = False
        get_settings().pr_code_suggestions.enable_chat_text = False
        get_settings().pr_code_suggestions.enable_help_text = False
        get_settings().pr_code_suggestions.persistent_comment = False
        get_settings().pr_code_suggestions.dual_publishing_score_threshold = 0

        get_settings().set("config.output_run_metadata", False)
        await suggestions.run()
        without_metadata = suggestions.git_provider.publish_comment.call_args[0][0]

        suggestions.git_provider.publish_comment.reset_mock()

        get_settings().set("config.output_run_metadata", True)
        await suggestions.run()
        with_metadata = suggestions.git_provider.publish_comment.call_args[0][0]

        assert "🔎 PR-Agent run metadata" not in without_metadata
        assert "🔎 PR-Agent run metadata" in with_metadata
    finally:
        restore_settings(snapshot)
