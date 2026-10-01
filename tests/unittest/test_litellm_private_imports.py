"""Tripwire tests for the guarded private LiteLLM imports.

``cloud_auth`` and ``litellm_ai_handler`` import a few private LiteLLM symbols inside
``try: ... except ImportError: name = None`` blocks and only check them at call time. A
LiteLLM release can move or delete a private symbol without touching the proxy HTTP API
(e.g. ``litellm.utils.prompt_token_calculator`` was dropped in 1.100.0), so CI can stay
green while production raises on the first relevant call. These tests fail the moment any
guarded name resolves to ``None``.
"""

import pytest

import pr_agent.algo.ai_handlers.cloud_auth as cloud_auth
import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler

# What breaks at runtime when each guarded LiteLLM symbol goes missing.
_CLOUD_AUTH_GUARDED = {
    "AnthropicModelInfo": (
        "Anthropic API-key/auth-token resolution returns None and Claude calls lose their auth headers"
    ),
    "JSONProviderRegistry": (
        "openai-compatible provider registry (list_providers/get) is unavailable, breaking the JSON provider path"
    ),
    "_get_model_info_helper": (
        "model-info lookup trips the runtime guard RuntimeError, breaking model fallback and capability checks"
    ),
    "BedrockMantleAuthMixin": "Bedrock Mantle request signing is skipped and Bedrock calls lose their auth signature",
}

_HANDLER_GUARDED = {
    **_CLOUD_AUTH_GUARDED,
    "MANTLE_HOST_RE": "Mantle endpoint detection is skipped, so the Mantle auth header is never applied",
}


@pytest.mark.parametrize("name", sorted(_CLOUD_AUTH_GUARDED))
def test_cloud_auth_guarded_litellm_imports_are_present(name):
    assert getattr(cloud_auth, name, None) is not None, (
        f"{name} is missing from cloud_auth; {_CLOUD_AUTH_GUARDED[name]}"
    )


@pytest.mark.parametrize("name", sorted(_HANDLER_GUARDED))
def test_litellm_handler_guarded_litellm_imports_are_present(name):
    assert getattr(litellm_handler, name, None) is not None, (
        f"{name} is missing from litellm_ai_handler; {_HANDLER_GUARDED[name]}"
    )
