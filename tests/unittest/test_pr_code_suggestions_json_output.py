import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.algo.review_json_output import review_json_output_path
from pr_agent.algo.run_details import add_token_usage
from pr_agent.algo.types import FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.tools import pr_code_suggestions as pr_code_suggestions_module
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_SETTINGS = (
    "config.publish_output",
    "config.publish_output_progress",
    "config.is_auto_command",
    "pr_code_suggestions.committable_code_suggestions",
    "pr_code_suggestions.dual_publishing_score_threshold",
    "pr_code_suggestions.persistent_comment",
    "github.publish_as_check_run",
)

_SUGGESTION = {
    "score": 7,
    "relevant_file": "app.py",
    "relevant_lines_start": 1,
    "relevant_lines_end": 1,
    "label": "maintainability",
    "one_sentence_summary": "Use a constant",
}


def _make_tool():
    provider = MagicMock()
    provider.should_publish_improve_as_thread.return_value = False
    provider.get_files.return_value = [object()]
    provider.is_supported.return_value = True
    provider.get_latest_commit_url.return_value = "https://example.invalid/commit/abcdef1234567890"
    provider.diff_files = [
        FilePatchInfo(
            base_file="line1\nline2\n",
            head_file="line1\nline2-changed\n",
            patch="@@ -1,2 +1,2 @@\n line1\n+line2-changed\n",
            filename="app.py",
        )
    ]
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = provider
    tool.pr_url = "https://example.invalid/pull/1"
    tool.progress_response = None
    tool.progress = "progress body"
    tool.incremental = SimpleNamespace(is_incremental=False)
    tool.generate_summarized_suggestions = MagicMock(return_value="summary")
    return tool


@pytest.fixture
def settings():
    snapshot = snapshot_settings(_TRACKED_SETTINGS)
    current = get_settings()
    current.config.publish_output_progress = False
    current.config.is_auto_command = False
    current.github.publish_as_check_run = False
    current.pr_code_suggestions.committable_code_suggestions = False
    current.pr_code_suggestions.dual_publishing_score_threshold = -1
    current.pr_code_suggestions.persistent_comment = False
    try:
        yield current
    finally:
        restore_settings(snapshot)


@pytest.fixture
def json_output(tmp_path):
    path = tmp_path / "improve.json"
    token = review_json_output_path.set(str(path))
    try:
        yield path
    finally:
        review_json_output_path.reset(token)


def _answer_with(monkeypatch, data, usage=None):
    async def answer(*args, **kwargs):
        if usage:
            add_token_usage(usage)
        return data

    monkeypatch.setattr(pr_code_suggestions_module, "retry_with_fallback_models", AsyncMock(side_effect=answer))


@pytest.mark.asyncio
async def test_writes_suggestions_and_usage_when_publishing_is_off(monkeypatch, settings, json_output):
    settings.config.publish_output = False
    _answer_with(
        monkeypatch,
        {"code_suggestions": [dict(_SUGGESTION)]},
        {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
    )

    await _make_tool().run()

    assert json.loads(json_output.read_text()) == {
        "code_suggestions": [_SUGGESTION],
        "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
    }


@pytest.mark.asyncio
async def test_writes_an_empty_list_when_there_are_no_suggestions(monkeypatch, settings, json_output):
    settings.config.publish_output = False
    _answer_with(monkeypatch, {"code_suggestions": []})

    await _make_tool().run()

    assert json.loads(json_output.read_text()) == {"code_suggestions": [], "usage": {}}


@pytest.mark.asyncio
async def test_writes_the_published_suggestions_after_publishing(monkeypatch, settings, json_output):
    settings.config.publish_output = True
    tool = _make_tool()

    def publish_comment(*args, **kwargs):
        assert not json_output.exists()

    tool.git_provider.publish_comment.side_effect = publish_comment
    _answer_with(monkeypatch, {"code_suggestions": [dict(_SUGGESTION)]})

    await tool.run()

    tool.git_provider.publish_comment.assert_called()
    assert json.loads(json_output.read_text())["code_suggestions"] == [_SUGGESTION]


@pytest.mark.asyncio
async def test_writes_nothing_without_a_json_output_path(monkeypatch, settings, tmp_path):
    settings.config.publish_output = False
    write = MagicMock()
    monkeypatch.setattr(pr_code_suggestions_module, "write_review_json_output", write)
    _answer_with(monkeypatch, {"code_suggestions": [dict(_SUGGESTION)]})

    await _make_tool().run()

    write.assert_not_called()


@pytest.mark.asyncio
async def test_writes_nothing_when_suggestions_fail(monkeypatch, settings, json_output):
    settings.config.publish_output = False
    monkeypatch.setattr(
        pr_code_suggestions_module,
        "retry_with_fallback_models",
        AsyncMock(side_effect=RuntimeError("model unavailable")),
    )

    await _make_tool().run()

    assert not json_output.exists()
