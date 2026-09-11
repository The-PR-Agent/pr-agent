"""
Regression test for issue #3309:
config.reasoning_effort must be forwarded for Gemini 3.x models.

SUPPORT_REASONING_EFFORT_MODELS previously contained only gemini-2.5-pro and
gemini-2.5-flash. Gemini 3.x models (gemini-3.1-flash, gemini-3.1-pro,
gemini-3.5-pro, gemini-3.8-flash) were in MAX_TOKENS but absent from the
allowlist, so any configured reasoning_effort was silently dropped.
"""
import pytest
from pr_agent.algo import SUPPORT_REASONING_EFFORT_MODELS


GEMINI_3X_MODELS = [
    "gemini-3.1-flash",
    "gemini-3.1-pro",
    "gemini-3.5-pro",
    "gemini-3.8-flash",
]

PROVIDER_PREFIXES = ["gemini/", "vertex_ai/", "openrouter/google/"]


@pytest.mark.parametrize("model", GEMINI_3X_MODELS)
def test_gemini3_bare_name_in_support_list(model):
    """Bare Gemini 3.x model names must be in SUPPORT_REASONING_EFFORT_MODELS."""
    assert model in SUPPORT_REASONING_EFFORT_MODELS, (
        f"{model!r} is missing from SUPPORT_REASONING_EFFORT_MODELS; "
        "reasoning_effort will be silently dropped for this model"
    )


@pytest.mark.parametrize("model", GEMINI_3X_MODELS)
@pytest.mark.parametrize("prefix", PROVIDER_PREFIXES)
def test_gemini3_prefixed_form_matches(model, prefix):
    """Provider-prefixed forms must match via the endswith check used in the handler."""
    prefixed = prefix + model
    matched = any(
        prefixed == m or prefixed.endswith("/" + m)
        for m in SUPPORT_REASONING_EFFORT_MODELS
    )
    assert matched, (
        f"{prefixed!r} does not match any entry in SUPPORT_REASONING_EFFORT_MODELS; "
        "reasoning_effort will be silently dropped when the model is referenced with a provider prefix"
    )


def test_gemini25_still_present():
    """Regression guard: gemini-2.5 entries must not be removed."""
    assert "gemini-2.5-pro" in SUPPORT_REASONING_EFFORT_MODELS
    assert "gemini-2.5-flash" in SUPPORT_REASONING_EFFORT_MODELS
