"""Shared scaffolding for the run-details command coverage tests.

Tests built on these helpers drive the tool's real run() through the real
retry_with_fallback_models, stubbing only the per-model prediction call, so the recorded
model (and fallback flag) comes from the production recording path. Each test module wraps
its tests in ``command_run_settings()`` through a module-level autouse fixture.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock

from pr_agent.algo.run_details import init_run_details, record_model_used
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

TITLE = "⚙️ Agent run details"
PRIMARY = "openai/primary-model"
BACKUP = "openai/backup-model"

_TRACKED_KEYS = (
    "config.model",
    "config.model_weak",
    "config.fallback_models",
    "config.output_run_details",
    "config.output_run_cost",
    "config.output_relevant_configurations",
    "config.publish_output",
    "config.propagate_tool_errors",
    "model_routing.enable",
    "openai.deployment_id",
    "openai.fallback_deployments",
    "comment_id",
    "data",
    "ask_diff_hunk",
    "line_start",
    "line_end",
    "side",
    "file_name",
    "pr_questions.enable_help_text",
    "pr_questions.use_conversation_history",
)


@contextmanager
def command_run_settings():
    """Apply a primary/fallback model chain with run details enabled, then restore settings."""
    snapshot = snapshot_settings(_TRACKED_KEYS)
    settings = get_settings()
    settings.set("config.model", PRIMARY)
    settings.set("config.model_weak", "")
    settings.set("config.fallback_models", [BACKUP])
    settings.set("config.output_run_details", True)
    settings.set("config.output_run_cost", False)
    settings.set("config.output_relevant_configurations", False)
    settings.set("config.publish_output", True)
    settings.set("config.propagate_tool_errors", True)
    settings.set("model_routing.enable", False)
    settings.set("openai.deployment_id", None)
    settings.set("openai.fallback_deployments", [])
    settings.set("comment_id", "")
    try:
        yield settings
    finally:
        restore_settings(snapshot)


def _provider(gfm=True):
    provider = MagicMock()
    provider.is_supported.side_effect = lambda capability: gfm and capability == "gfm_markdown"
    provider.supports_threaded_pr_questions.return_value = False
    provider.supports_line_question_history.return_value = False
    return provider


def _seed_stale_fallback_run():
    """Leave a previous command's fallback model in the current context."""
    init_run_details()
    record_model_used("openai/stale-model", is_fallback=True)


def _published_bodies(provider):
    """Every non-temporary comment and thread reply the tool published, in order."""
    bodies = [c.args[0] for c in provider.publish_comment.call_args_list if not c.kwargs.get("is_temporary")]
    bodies += [c.args[1] for c in provider.reply_to_comment_from_comment_id.call_args_list]
    return bodies


def _model_stub(result, fail_primary=False):
    """One prediction attempt per model; optionally fail the primary so the fallback answers."""
    attempts = []

    async def attempt(model):
        attempts.append(model)
        if fail_primary and model == PRIMARY:
            raise TimeoutError("primary unavailable")
        return result

    attempt.attempts = attempts
    return attempt
