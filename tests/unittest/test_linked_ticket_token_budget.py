"""Regression tests for bounded linked-ticket prompt context."""

import copy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.pr_processing import (
    OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD,
    generate_full_patch,
)
from pr_agent.algo.types import EDIT_TYPE
from pr_agent.config_loader import get_settings
from pr_agent.tools import pr_description as pr_description_module
from pr_agent.tools import pr_reviewer as pr_reviewer_module
from pr_agent.tools import ticket_pr_compliance_check as tickets_module
from pr_agent.tools.pr_description import PRDescription
from pr_agent.tools.pr_reviewer import PRReviewer
from pr_agent.tools.ticket_pr_compliance_check import fit_related_tickets_to_prompt_budget


class _PromptCountingTokenHandler:
    """A deterministic stand-in whose ticket sizes do not depend on tiktoken."""

    baseline_tokens = 100

    def __init__(self, _pr, vars_, _system, _user, model=None):
        self.vars = copy.deepcopy(vars_)
        self.model = model
        self.prompt_tokens = self.baseline_tokens + sum(
            ticket["_test_tokens"] for ticket in self.vars.get("related_tickets", [])
        )


def _tickets(count, tokens=200):
    return [
        {
            "ticket_id": index,
            "title": f"Ticket {index}",
            "body": ("ticket context " * (tokens // 15 + 1))[:tokens],
            "labels": ["bug"],
            "nested": {"index": index},
            "_test_tokens": tokens,
        }
        for index in range(count)
    ]


@pytest.fixture
def prompt_budget(monkeypatch):
    """Use a 1,500-token diff/output reserve with deterministic prompt sizes."""

    def configure(model_limits):
        monkeypatch.setattr(tickets_module, "TokenHandler", _PromptCountingTokenHandler)
        monkeypatch.setattr(tickets_module, "get_max_tokens", lambda model: model_limits[model])

    return configure


def test_ticket_payload_keeps_exact_prefix_that_fits_prompt_budget(prompt_budget):
    # max=3,500 -> max(100, 3,500 - 2*1,500) = 500.  Two 200-token tickets fit exactly.
    prompt_budget({"model": 3500})
    raw_vars = {"related_tickets": _tickets(3)}

    prompt_vars, handler = fit_related_tickets_to_prompt_budget(
        object(), raw_vars, "system", "{{ related_tickets }}", "model"
    )

    assert [ticket["ticket_id"] for ticket in prompt_vars["related_tickets"]] == [0, 1]
    assert handler.prompt_tokens == 500
    assert handler.prompt_tokens <= 3500 - 2 * OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD


def test_under_budget_ticket_payload_is_preserved_without_aliasing_raw_cache(prompt_budget):
    prompt_budget({"model": 4000})
    raw_tickets = _tickets(3)
    raw_vars = {"related_tickets": raw_tickets, "title": "Original title"}
    raw_before = copy.deepcopy(raw_vars)

    prompt_vars, handler = fit_related_tickets_to_prompt_budget(
        object(), raw_vars, "system", "{{ related_tickets }}", "model"
    )

    assert prompt_vars == raw_before
    assert prompt_vars is not raw_vars
    assert prompt_vars["related_tickets"] is not raw_tickets
    assert prompt_vars["related_tickets"][0] is not raw_tickets[0]
    assert prompt_vars["related_tickets"][0]["nested"] is not raw_tickets[0]["nested"]
    prompt_vars["related_tickets"][0]["nested"]["changed"] = True
    assert raw_vars == raw_before
    assert handler.vars == raw_before
    assert handler.vars is not raw_vars


def test_oversized_ticket_payload_keeps_diff_budget_and_raw_cache(monkeypatch):
    monkeypatch.setattr(tickets_module, "get_max_tokens", lambda _model: 32000)
    raw_vars = {"related_tickets": _tickets(33, tokens=10000)}
    raw_before = copy.deepcopy(raw_vars)

    prompt_vars, handler = fit_related_tickets_to_prompt_budget(
        object(),
        raw_vars,
        "Review this pull request.",
        "{% for ticket in related_tickets %}{{ ticket.body }}{% endfor %}",
        "gpt-4o",
    )

    assert 0 < len(prompt_vars["related_tickets"]) < len(raw_vars["related_tickets"])
    assert handler.prompt_tokens <= 32000 - 2 * OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD
    assert raw_vars == raw_before

    patch = "@@ -1 +1 @@\n-old_value\n+new_value"
    patch_tokens = handler.count_tokens(f"\n\n{patch}")
    max_tokens = handler.prompt_tokens + patch_tokens + OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD
    file_dict = {
        "src/app.py": {
            "patch": patch,
            "tokens": patch_tokens,
            "edit_type": EDIT_TYPE.MODIFIED,
        }
    }

    result = generate_full_patch(True, file_dict, max_tokens, ["src/app.py"], handler)

    assert result[1] == [f"\n\n{patch}"]
    assert result[2] == []


def test_smaller_fallback_model_recalculates_from_raw_tickets(prompt_budget):
    prompt_budget({"primary": 4100, "fallback": 3500})
    raw_vars = {"related_tickets": _tickets(5)}

    primary_vars, primary_handler = fit_related_tickets_to_prompt_budget(
        object(), raw_vars, "system", "{{ related_tickets }}", "primary"
    )
    fallback_vars, fallback_handler = fit_related_tickets_to_prompt_budget(
        object(), raw_vars, "system", "{{ related_tickets }}", "fallback"
    )

    assert len(primary_vars["related_tickets"]) == 5
    assert len(fallback_vars["related_tickets"]) == 2
    assert primary_handler.model == "primary"
    assert fallback_handler.model == "fallback"
    assert raw_vars["related_tickets"] == _tickets(5)


def test_baseline_overflow_uses_no_ticket_context(prompt_budget):
    _PromptCountingTokenHandler.baseline_tokens = 600
    prompt_budget({"model": 3500})
    raw_vars = {"related_tickets": _tickets(2)}
    try:
        prompt_vars, handler = fit_related_tickets_to_prompt_budget(
            object(), raw_vars, "system", "{{ related_tickets }}", "model"
        )
    finally:
        _PromptCountingTokenHandler.baseline_tokens = 100

    assert prompt_vars["related_tickets"] == []
    assert handler.prompt_tokens == 600
    assert raw_vars["related_tickets"] == _tickets(2)


def _make_tool(tool_name):
    provider = MagicMock()
    if tool_name == "review":
        tool = PRReviewer.__new__(PRReviewer)
        tool.pr_url = "https://example.test/pull/1"
        tool.incremental = SimpleNamespace(is_incremental=False)
        tool.remaining_files_list = []
        module = pr_reviewer_module
    else:
        tool = PRDescription.__new__(PRDescription)
        tool.pr_id = "1"
        tool.user_description = ""
        tool.keys_fix = []
        module = pr_description_module
    tool.git_provider = provider
    tool.vars = {"related_tickets": _tickets(33)}
    tool._raw_prompt_vars = copy.deepcopy(tool.vars)
    tool.token_handler = SimpleNamespace(prompt_tokens=0)
    tool.prediction = None
    return tool, module


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["review", "description"])
async def test_tools_use_the_same_bounded_ticket_vars_for_packing_and_rendering(monkeypatch, tool_name):
    tool, module = _make_tool(tool_name)
    raw_snapshot = copy.deepcopy(tool._raw_prompt_vars)
    prompt_vars = {"related_tickets": _tickets(2)}
    handler = SimpleNamespace(prompt_tokens=500)
    helper_calls = []
    diff_handlers = []
    rendered_vars = []

    def fit_payload(pr, raw_vars, _system, _user, model):
        helper_calls.append((pr, raw_vars, model))
        return prompt_vars, handler

    def get_diff(_provider, token_handler, _model, **_kwargs):
        diff_handlers.append(token_handler)
        return "@@ -1 +1 @@\n-old\n+new"

    async def get_prediction(_model, *_args, **_kwargs):
        rendered_vars.append(tool.vars)
        return "prediction"

    monkeypatch.setattr(module, "fit_related_tickets_to_prompt_budget", fit_payload)
    monkeypatch.setattr(module, "get_pr_diff", get_diff)
    monkeypatch.setattr(tool, "_get_prediction", get_prediction)
    if tool_name == "description":
        monkeypatch.setattr(module, "build_repo_context", lambda _provider: "")

    settings = get_settings()
    original_semantic_files_types = settings.pr_description.enable_semantic_files_types
    settings.pr_description.enable_semantic_files_types = False
    try:
        await tool._prepare_prediction("fallback-model")
    finally:
        settings.pr_description.enable_semantic_files_types = original_semantic_files_types

    assert helper_calls == [(tool.git_provider.pr, tool._raw_prompt_vars, "fallback-model")]
    assert diff_handlers == [handler]
    assert rendered_vars == [prompt_vars]
    assert tool.vars is prompt_vars
    assert tool.token_handler is handler
    assert tool._raw_prompt_vars == raw_snapshot


@pytest.mark.asyncio
async def test_description_large_pr_fits_each_prompt_from_raw_tickets(monkeypatch):
    tool, module = _make_tool("description")
    raw_snapshot = copy.deepcopy(tool._raw_prompt_vars)
    prompt_vars = [
        {"related_tickets": _tickets(33)},
        {"related_tickets": _tickets(2)},
        {"related_tickets": _tickets(1)},
    ]
    handlers = [
        SimpleNamespace(prompt_tokens=0),
        SimpleNamespace(prompt_tokens=500),
        SimpleNamespace(prompt_tokens=300, encoder=SimpleNamespace(encode=lambda _text: [])),
    ]
    fit_calls = []
    packed_handlers = []
    prediction_calls = []

    def fit_payload(_pr, raw_vars, _system, _user, model):
        call_index = len(fit_calls)
        fit_calls.append((raw_vars, model))
        return prompt_vars[call_index], handlers[call_index]

    def get_multiple_patches(_provider, token_handler, _model):
        packed_handlers.append(token_handler)
        return ([["@@ -1 +1 @@\n-old\n+new"]], [10], [], [], {}, [[]])

    async def get_prediction(_model, patches_diff=None, prompt=None):
        prediction_calls.append((prompt, patches_diff, tool.vars))
        if prompt == "pr_description_only_files_prompts":
            return "pr_files:\n- filename: src/app.py"
        return "title: Test\ndescription: Test"

    monkeypatch.setattr(module, "fit_related_tickets_to_prompt_budget", fit_payload)
    monkeypatch.setattr(module, "get_pr_diff", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(module, "get_pr_diff_multiple_patchs", get_multiple_patches)
    monkeypatch.setattr(module, "get_max_tokens", lambda _model: 32000)
    monkeypatch.setattr(tool, "_get_prediction", get_prediction)
    monkeypatch.setattr(tool, "extend_uncovered_files", lambda _prediction: _empty_string())

    settings = get_settings()
    original_large_pr_handling = settings.pr_description.enable_large_pr_handling
    settings.pr_description.enable_large_pr_handling = True
    try:
        await tool._prepare_prediction("fallback-model")
    finally:
        settings.pr_description.enable_large_pr_handling = original_large_pr_handling

    assert fit_calls == [
        (tool._raw_prompt_vars, "fallback-model"),
        (tool._raw_prompt_vars, "fallback-model"),
        (tool._raw_prompt_vars, "fallback-model"),
    ]
    assert tool._raw_prompt_vars == raw_snapshot
    assert packed_handlers == [handlers[1]]
    assert prediction_calls[0] == (
        "pr_description_only_files_prompts",
        "@@ -1 +1 @@\n-old\n+new",
        prompt_vars[1],
    )
    assert prediction_calls[1][0] == "pr_description_only_description_prompts"
    assert prediction_calls[1][2] is prompt_vars[2]


async def _empty_string():
    return ""
