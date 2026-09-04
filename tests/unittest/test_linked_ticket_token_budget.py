"""Regression tests for linked-ticket prompt token accounting."""

import copy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.pr_processing import OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD, generate_full_patch
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.types import EDIT_TYPE
from pr_agent.config_loader import get_settings
from pr_agent.tools import pr_description as pr_description_module
from pr_agent.tools import pr_reviewer as pr_reviewer_module
from pr_agent.tools.pr_description import PRDescription
from pr_agent.tools.pr_reviewer import PRReviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


class _RecordingTokenHandler:
    instances = []

    def __init__(self, _pr, vars_, _system, _user):
        self.related_tickets = copy.deepcopy(vars_.get("related_tickets"))
        self.prompt_tokens = len(str(self.related_tickets))
        self.instances.append(self)


def _make_tool(tool_name, provider, initial_handler):
    if tool_name == "review":
        provider.get_files.return_value = ["src/app.py"]
        tool = PRReviewer.__new__(PRReviewer)
        tool.pr_url = "https://example.test/pull/1"
        tool.incremental = SimpleNamespace(is_incremental=False)
        tool.vars = {"related_tickets": []}
        module = pr_reviewer_module
    else:
        tool = PRDescription.__new__(PRDescription)
        tool.pr_id = "1"
        tool.vars = {"related_tickets": ""}
        module = pr_description_module
    tool.git_provider = provider
    tool.token_handler = initial_handler
    tool.prediction = None
    return tool, module


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["review", "description"])
async def test_tool_refreshes_prompt_budget_after_ticket_extraction(monkeypatch, tool_name):
    settings_snapshot = snapshot_settings(
        (
            "config.publish_output",
            "config.is_auto_command",
            "config.propagate_tool_errors",
            "pr_description.enable_large_pr_handling",
        )
    )
    provider = MagicMock()
    initial_handler = SimpleNamespace(prompt_tokens=0)
    tool, module = _make_tool(tool_name, provider, initial_handler)
    tickets = [{"title": "Bug", "body": "Acceptance criteria and reproduction details"}]
    captured_handlers = []

    async def extract_tickets(_provider, vars_):
        vars_["related_tickets"] = tickets

    def capture_handler(_provider, token_handler, _model, **_kwargs):
        captured_handlers.append(token_handler)
        return ""

    async def run_once(prepare_prediction, *_args, **_kwargs):
        await prepare_prediction("gpt-4o")

    _RecordingTokenHandler.instances = []
    monkeypatch.setattr(module, "extract_and_cache_pr_tickets", extract_tickets)
    monkeypatch.setattr(module, "TokenHandler", _RecordingTokenHandler)
    monkeypatch.setattr(module, "get_pr_diff", capture_handler)
    monkeypatch.setattr(module, "retry_with_fallback_models", run_once)

    settings = get_settings()
    settings.config.publish_output = False
    settings.config.is_auto_command = False
    settings.config.propagate_tool_errors = False
    settings.pr_description.enable_large_pr_handling = False

    try:
        await tool.run()
    finally:
        restore_settings(settings_snapshot)

    assert len(_RecordingTokenHandler.instances) == 1
    refreshed_handler = _RecordingTokenHandler.instances[0]
    assert captured_handlers == [refreshed_handler]
    assert tool.token_handler is refreshed_handler
    assert refreshed_handler is not initial_handler
    assert tool.vars["related_tickets"] is tickets
    assert refreshed_handler.related_tickets == tickets
    assert refreshed_handler.related_tickets is not tickets
    assert refreshed_handler.prompt_tokens > initial_handler.prompt_tokens


def test_linked_ticket_tokens_reduce_the_available_diff_budget():
    prompt = "Linked tickets:\n{{ related_tickets }}"
    base_handler = TokenHandler(object(), {"related_tickets": []}, "Review this pull request.", prompt)
    ticket_handler = TokenHandler(
        object(),
        {"related_tickets": [{"body": "acceptance criteria " * 20}]},
        "Review this pull request.",
        prompt,
    )
    patch = "@@ -1 +1 @@\n-old_value\n+new_value"
    patch_tokens = base_handler.count_tokens(f"\n\n{patch}")
    max_tokens = base_handler.prompt_tokens + patch_tokens + OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD
    file_dict = {
        "src/app.py": {
            "patch": patch,
            "tokens": patch_tokens,
            "edit_type": EDIT_TYPE.MODIFIED,
        }
    }

    base_result = generate_full_patch(True, file_dict, max_tokens, ["src/app.py"], base_handler)
    ticket_result = generate_full_patch(True, file_dict, max_tokens, ["src/app.py"], ticket_handler)

    assert ticket_handler.prompt_tokens > base_handler.prompt_tokens
    assert base_result[1] == [f"\n\n{patch}"]
    assert base_result[2] == []
    assert ticket_result[1] == []
    assert ticket_result[2] == ["src/app.py"]
