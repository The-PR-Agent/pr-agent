import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from pr_agent.algo.review_json_output import review_json_output_path
from pr_agent.algo.run_details import add_token_usage
from pr_agent.config_loader import get_settings
from pr_agent.tools import pr_description as pr_description_module
from pr_agent.tools.pr_description import PRDescription
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_SETTINGS = (
    "config.publish_output",
    "config.publish_output_progress",
    "config.is_auto_command",
    "pr_description.enable_semantic_files_types",
    "pr_description.publish_labels",
    "pr_description.use_description_markers",
)

_PREDICTION = {
    "title": "Add a login endpoint",
    "summary": {"type_of_change": ["new feature"]},
    "pr_files": [{"file": "app.py", "changes": ["Add route"]}],
}


def _make_tool():
    provider = MagicMock()
    provider.is_supported.return_value = False
    tool = PRDescription.__new__(PRDescription)
    tool.git_provider = provider
    tool.pr_id = "example/repo/1"
    tool.vars = {"title": "raw", "diff": "", "description": ""}
    tool.user_description = ""
    tool.prediction = None
    tool.progress_response = None
    tool._prepare_labels = MagicMock(return_value=[])
    tool.keys_fix = []
    tool._prepare_pr_answer = MagicMock(return_value=("T", "B", "W"))
    tool._get_description_coverage_footer = MagicMock(return_value="")
    return tool


@pytest.fixture
def settings():
    snapshot = snapshot_settings(_TRACKED_SETTINGS)
    current = get_settings()
    current.config.publish_output = False
    current.config.publish_output_progress = False
    current.config.is_auto_command = False
    current.pr_description.enable_semantic_files_types = False
    current.pr_description.publish_labels = False
    current.pr_description.use_description_markers = False
    try:
        yield current
    finally:
        restore_settings(snapshot)


@pytest.fixture
def json_output(tmp_path):
    path = tmp_path / "describe.json"
    token = review_json_output_path.set(str(path))
    try:
        yield path
    finally:
        review_json_output_path.reset(token)


def _stub_model(monkeypatch, tool, usage=None):
    """Stand in for retry_with_fallback_models(_prepare_prediction, ...): record the prediction
    like _prepare_prediction would, and optionally account the run's token usage."""
    async def fake_retry(func, *args, **kwargs):
        if usage:
            add_token_usage(usage)
        tool.prediction = yaml.safe_dump(_PREDICTION)
        return tool.prediction

    monkeypatch.setattr(pr_description_module, "retry_with_fallback_models", fake_retry)
    monkeypatch.setattr(pr_description_module, "extract_and_cache_pr_tickets", AsyncMock())


@pytest.mark.asyncio
async def test_writes_description_and_usage_when_publishing_is_off(monkeypatch, settings, json_output):
    tool = _make_tool()
    _stub_model(monkeypatch, tool, {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42})

    await tool.run()

    written = json.loads(json_output.read_text())
    assert written["title"] == "Add a login endpoint"
    assert written["pr_files"] == [{"file": "app.py", "changes": ["Add route"]}]
    assert written["usage"] == {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42}


@pytest.mark.asyncio
async def test_no_file_written_without_a_json_output_path(monkeypatch, settings, tmp_path):
    tool = _make_tool()
    _stub_model(monkeypatch, tool)

    await tool.run()

    assert list(tmp_path.iterdir()) == []
