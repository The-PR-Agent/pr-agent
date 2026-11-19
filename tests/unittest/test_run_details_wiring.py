from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.run_details import init_run_details, record_ai_call, record_model_used
from pr_agent.commands.pr_description import PRDescription
from pr_agent.commands.pr_reviewer import PRReviewer
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_KEYS_REVIEW = (
    "config.output_run_details",
    "config.output_run_cost",
    "config.publish_output",
    "config.is_auto_command",
    "data",
    "pr_reviewer.enable_help_text",
)
_TRACKED_KEYS_DESCRIPTION = (
    "config.output_run_details",
    "config.publish_output",
    "config.is_auto_command",
    "data",
    "pr_description.enable_semantic_files_types",
    "pr_description.publish_labels",
    "pr_description.use_description_markers",
    "pr_description.enable_help_text",
    "pr_description.enable_help_comment",
)


class _Usage:
    def __init__(self, prompt_tokens, completion_tokens, total_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


def _seed_run_details():
    init_run_details()
    record_model_used("openai/gpt-5.4", is_fallback=False)
    record_ai_call(_Usage(10, 2, 12))


def _seeded_init_run_details():
    _seed_run_details()


async def _noop_async(*_args, **_kwargs):
    return None


def test_flag_defaults_to_false():
    assert get_settings().config.get("output_run_details", None) is False
    assert get_settings().config.get("output_run_cost", None) is False


@pytest.mark.asyncio
async def test_pr_reviewer_appends_run_details_only_when_enabled(monkeypatch):
    snapshot = snapshot_settings(_TRACKED_KEYS_REVIEW)
    try:
        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.pr_url = "https://example/pr/1"
        reviewer.vars = {}
        reviewer.prediction = """
review:
  estimated_effort_to_review_[1-5]: "2"
  security_concerns: "No"
"""
        reviewer.incremental = SimpleNamespace(is_incremental=False)
        reviewer.remaining_files_list = []
        reviewer.git_provider = MagicMock()
        reviewer.git_provider.get_files.return_value = ["changed.py"]
        reviewer.git_provider.is_supported.side_effect = lambda cap: cap == "gfm_markdown"
        reviewer.git_provider.get_diff_files.return_value = []

        # Seeding through the command's own init hook is what makes this a wiring test:
        # drop init_run_details() from run() and the seed never lands, so the
        # section cannot render and this test fails.
        monkeypatch.setattr("pr_agent.commands.pr_reviewer.init_run_details", _seeded_init_run_details)
        monkeypatch.setattr("pr_agent.commands.pr_reviewer.extract_and_cache_pr_tickets", _noop_async)
        monkeypatch.setattr("pr_agent.commands.pr_reviewer.retry_with_fallback_models", _noop_async)

        get_settings().set("config.publish_output", False)
        get_settings().set("config.is_auto_command", False)
        get_settings().pr_reviewer.enable_help_text = False

        get_settings().set("config.output_run_cost", True)
        get_settings().set("config.output_run_details", False)
        await reviewer.run()
        without_details = get_settings().data["artifact"]

        get_settings().set("config.output_run_details", True)
        await reviewer.run()
        with_details = get_settings().data["artifact"]

        assert "⚙️ Agent run details" not in without_details
        assert "Estimated API cost" not in without_details
        assert "⚙️ Agent run details" in with_details
        assert "Estimated API cost: unavailable" in with_details
    finally:
        restore_settings(snapshot)


@pytest.mark.asyncio
async def test_pr_description_appends_run_details_only_when_enabled(monkeypatch):
    snapshot = snapshot_settings(_TRACKED_KEYS_DESCRIPTION)
    try:
        description = PRDescription.__new__(PRDescription)
        description.pr_id = "1"
        description.vars = {}
        description.prediction = "prediction"
        description.file_label_dict = {}
        description.data = {}  # run() hands the parsed dict to the JSON writer after _prepare_data
        description.git_provider = MagicMock()
        description.git_provider.is_supported.side_effect = lambda cap: cap == "gfm_markdown"

        description._prepare_data = MagicMock()
        description._prepare_pr_answer = MagicMock(return_value=("AI title", "Base description body", ""))

        monkeypatch.setattr("pr_agent.commands.pr_description.init_run_details", _seeded_init_run_details)
        monkeypatch.setattr("pr_agent.commands.pr_description.extract_and_cache_pr_tickets", _noop_async)
        monkeypatch.setattr("pr_agent.commands.pr_description.retry_with_fallback_models", _noop_async)

        get_settings().set("config.publish_output", False)
        get_settings().set("config.is_auto_command", False)
        get_settings().pr_description.enable_semantic_files_types = False
        get_settings().pr_description.publish_labels = False
        get_settings().pr_description.use_description_markers = False
        get_settings().pr_description.enable_help_text = False
        get_settings().pr_description.enable_help_comment = False

        get_settings().set("config.output_run_details", False)
        await description.run()
        without_details = get_settings().data["artifact"]

        get_settings().set("config.output_run_details", True)
        await description.run()
        with_details = get_settings().data["artifact"]

        assert "⚙️ Agent run details" not in without_details
        assert "⚙️ Agent run details" in with_details
    finally:
        restore_settings(snapshot)
