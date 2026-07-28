from unittest.mock import MagicMock

import pytest

from pr_agent.algo.run_metadata import init_run_metadata, record_ai_call, record_model_used
from pr_agent.algo.utils import show_run_metadata
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_KEYS = ("config.output_run_metadata",)


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


def _render_if_enabled(git_provider):
    """Mirror the production gate used by the three tools."""
    if get_settings().config.get("output_run_metadata", False):
        return show_run_metadata(git_provider.is_supported("gfm_markdown"))
    return ""


def test_flag_defaults_to_false():
    assert get_settings().config.get("output_run_metadata", None) is False


def test_section_rendered_only_when_flag_enabled():
    snapshot = snapshot_settings(_TRACKED_KEYS)
    try:
        init_run_metadata()
        record_model_used("openai/gpt-5.4", is_fallback=False)
        record_ai_call(_Usage(10, 2, 12))
        git_provider = MagicMock()
        git_provider.is_supported.side_effect = lambda cap: cap == "gfm_markdown"

        get_settings().set("config.output_run_metadata", False)
        assert _render_if_enabled(git_provider) == ""

        get_settings().set("config.output_run_metadata", True)
        output = _render_if_enabled(git_provider)
        assert "🔎 PR-Agent run metadata" in output
        assert "Model: openai/gpt-5.4" in output
    finally:
        restore_settings(snapshot)
