"""Regression tests for verified /help_docs prompt sizing.

``/help_docs`` used to size the documentation text against a crude per-model
guess and dispatch the same prompt to every fallback model. Each model attempt
must instead be fitted against the exact rendered request so an over-budget
prompt is never sent.
"""

import math
from types import SimpleNamespace

import pytest

from pr_agent.algo.token_handler import TokenHandler
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_help_docs import PredictionPreparator, PRHelpDocs

_FIT_MODEL = "gpt-fit-test"


@pytest.fixture
def bounded_model():
    settings = get_settings()
    previous_model = settings.config.model
    previous_custom = settings.config.custom_model_max_tokens
    yield settings
    settings.set("config.model", previous_model)
    settings.set("config.custom_model_max_tokens", previous_custom)


def _preparator(window_tokens: int, snippets: str, bounded_model):
    bounded_model.set("config.model", _FIT_MODEL)
    bounded_model.set("config.custom_model_max_tokens", window_tokens)
    vars = {
        "docs_url": "https://example.com/org/repo",
        "question": "How do I configure this?",
        "snippets": snippets,
    }
    return PredictionPreparator(
        SimpleNamespace(),
        vars,
        get_settings().pr_help_docs_prompts.system,
        get_settings().pr_help_docs_prompts.user,
    )


def test_prediction_preparator_clips_snippets_to_verified_limit(bounded_model):
    full_snippets = "word " * 40_000
    preparator = _preparator(20_000, full_snippets, bounded_model)

    fitted = preparator._fit_snippets_for_model(_FIT_MODEL)

    assert fitted.input_tokens <= 20_000
    assert fitted.optional_text != full_snippets
    assert len(fitted.optional_text) < len(full_snippets)
    assert "(truncated)" in fitted.optional_text


def test_prediction_preparator_keeps_snippets_when_they_fit(bounded_model):
    short_snippets = "# Guide\n\nWelcome to the docs.\n"
    preparator = _preparator(200_000, short_snippets, bounded_model)

    fitted = preparator._fit_snippets_for_model(_FIT_MODEL)

    assert fitted.optional_text == short_snippets
    assert "(truncated)" not in fitted.optional_text


@pytest.mark.asyncio
async def test_prediction_preparator_dispatches_fitted_prompts(bounded_model):
    full_snippets = "word " * 40_000
    preparation = _preparator(20_000, full_snippets, bounded_model)
    dispatched = {}

    async def chat_completion(*, model, temperature, system, user):
        dispatched["model"] = model
        dispatched["system"] = system
        dispatched["user"] = user
        return "user_question: test", "stop"

    preparation.ai_handler.chat_completion = chat_completion

    response = await preparation(_FIT_MODEL)

    assert dispatched["model"] == _FIT_MODEL
    assert response == "user_question: test"
    assert "(truncated)" in dispatched["user"]
    assert len(dispatched["user"]) < len(full_snippets)


def test_prediction_preparator_raises_when_request_cannot_fit(bounded_model):
    preparator = _preparator(200, "", bounded_model)

    with pytest.raises(ValueError):
        preparator._fit_snippets_for_model(_FIT_MODEL)


def test_trim_docs_input_uses_verified_input_limit(bounded_model):
    bounded_model.set("config.model", _FIT_MODEL)
    bounded_model.set("config.custom_model_max_tokens", 20_000)
    tool = PRHelpDocs.__new__(PRHelpDocs)
    tool.vars = {
        "docs_url": "https://example.com/org/repo",
        "question": "How do I configure this?",
        "snippets": "",
    }
    tool.ai_handler = SimpleNamespace()
    tool.token_handler = TokenHandler(
        None,
        tool.vars,
        get_settings().pr_help_docs_prompts.system,
        get_settings().pr_help_docs_prompts.user,
    )

    big_docs = "word " * 40_000
    assert tool._trim_docs_input(big_docs, math.inf, only_return_if_trim_needed=True) is True

    trimmed = tool._trim_docs_input(big_docs, math.inf, only_return_if_trim_needed=False)
    assert isinstance(trimmed, str)
    assert trimmed != big_docs
    limit = tool._docs_input_token_limit()
    assert tool.token_handler.count_tokens(trimmed, force_accurate=True) <= limit
