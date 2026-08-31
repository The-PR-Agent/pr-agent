"""
Tests for the OpenRouter provider-routing / reasoning / output-cap controls in
LiteLLMAIHandler.chat_completion.

The [openrouter] settings (provider_only, provider_order, allow_fallbacks,
reasoning_effort, reasoning_max_tokens, max_tokens) are injected into the request
as `extra_body.provider`, `extra_body.reasoning` and `max_tokens`, but only for
models addressed as "openrouter/...". Registered reasoning models inherit the
global effort when no OpenRouter effort or budget is set; other models are no-op.
"""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import openai
import pytest
from litellm.llms.openrouter.chat.transformation import OpenrouterConfig
from litellm.utils import get_llm_provider, get_optional_params

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler

# Environment variables that LiteLLMAIHandler.__init__ reads or mutates: the AWS
# credential path (entered when AWS_USE_IMDS is set) writes the AWS_* variables,
# and OPENAI_API_KEY influences the litellm.api_key fallback.
_HANDLER_ENV_VARS = (
    "AWS_USE_IMDS",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION_NAME",
    "OPENAI_API_KEY",
)


@pytest.fixture(autouse=True)
def _restore_litellm_globals():
    """LiteLLMAIHandler.__init__ mutates global litellm/openai state and, when
    AWS_USE_IMDS is set, os.environ; snapshot and restore both, and isolate
    drop_params so parameter-validation tests are deterministic."""
    saved = (
        litellm.api_key,
        getattr(litellm, "openai_key", None),
        openai.api_key,
        litellm.drop_params,
    )
    saved_env = {name: os.environ.get(name) for name in _HANDLER_ENV_VARS}
    os.environ.pop("AWS_USE_IMDS", None)
    litellm.drop_params = False
    try:
        yield
    finally:
        litellm.api_key = saved[0]
        litellm.openai_key = saved[1]
        openai.api_key = saved[2]
        litellm.drop_params = saved[3]
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _make_settings(openrouter=None, reasoning_effort="medium", custom_llm_provider=""):
    """Minimal settings whose `.get("openrouter", ...)` returns the given dict."""
    openrouter = openrouter or {}
    return type("Settings", (), {
        "config": type("Config", (), {
            "reasoning_effort": reasoning_effort,
            "ai_timeout": 30,
            "custom_reasoning_model": False,
            "max_model_tokens": 32000,
            "verbosity_level": 0,
            "seed": -1,
            "get": lambda self, key, default=None: default,
        })(),
        "litellm": type("LiteLLM", (), {
            "custom_llm_provider": custom_llm_provider,
            "get": lambda self, key, default=None: default,
        })(),
        "get": lambda self, key, default=None: (openrouter if key == "openrouter" else default),
    })()


def _mock_response():
    mock = MagicMock()
    response = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    mock.__getitem__.side_effect = response.__getitem__
    mock.dict.return_value = response
    return mock


async def _run(monkeypatch, model, openrouter, reasoning_effort="medium", custom_llm_provider=""):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings(openrouter, reasoning_effort, custom_llm_provider),
    )
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
               new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        await handler.chat_completion(model=model, system="sys", user="usr")
    return mock_call.call_args[1]


class TestOpenRouterControls:

    @pytest.mark.asyncio
    async def test_provider_only_and_reasoning_effort_and_max_tokens(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {
            "provider_only": ["z-ai"],
            "reasoning_effort": "low",
            "max_tokens": 16000,
        })
        assert kwargs["extra_body"] == {"provider": {"only": ["z-ai"]}, "reasoning": {"effort": "low"}}
        assert kwargs["max_tokens"] == 16000

    @pytest.mark.asyncio
    async def test_provider_order_with_allow_fallbacks(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {
            "provider_order": ["z-ai", "novita"],
            "allow_fallbacks": False,
        })
        assert kwargs["extra_body"]["provider"] == {"order": ["z-ai", "novita"], "allow_fallbacks": False}

    @pytest.mark.asyncio
    async def test_provider_only_wins_over_order(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {
            "provider_only": ["z-ai"],
            "provider_order": ["novita"],
        })
        assert kwargs["extra_body"]["provider"] == {"only": ["z-ai"]}

    @pytest.mark.asyncio
    async def test_reasoning_none_disables(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {"reasoning_effort": "none"})
        assert kwargs["extra_body"]["reasoning"] == {"enabled": False}

    @pytest.mark.asyncio
    async def test_reasoning_max_tokens(self, monkeypatch):
        """Verify that a token budget suppresses the mutually exclusive effort control."""
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {
            "reasoning_effort": "high",
            "reasoning_max_tokens": 2048,
        })
        assert kwargs["extra_body"]["reasoning"] == {"max_tokens": 2048}

    @pytest.mark.asyncio
    async def test_no_config_is_noop(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {})
        assert "extra_body" not in kwargs
        assert "max_tokens" not in kwargs

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("model", "routed_model"),
        [
            ("openrouter/auto", "openrouter/openrouter/auto"),
            ("openrouter/free", "openrouter/openrouter/free"),
            ("openrouter/fusion", "openrouter/openrouter/fusion"),
            ("openrouter/pareto-code", "openrouter/openrouter/pareto-code"),
        ],
    )
    async def test_router_model_uses_openrouter_defaults(self, monkeypatch, model, routed_model):
        kwargs = await _run(monkeypatch, model, {})
        assert kwargs["model"] == routed_model
        assert "extra_body" not in kwargs
        assert "max_tokens" not in kwargs

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("custom_llm_provider", "expected_model", "expected_provider"),
        [
            (" OpenRouter ", "openrouter/openrouter/auto", "openrouter"),
            (" OpenAI ", "openrouter/auto", "openai"),
        ],
    )
    async def test_router_model_respects_custom_llm_provider(
        self,
        monkeypatch,
        custom_llm_provider,
        expected_model,
        expected_provider,
    ):
        kwargs = await _run(monkeypatch, "openrouter/auto", {}, custom_llm_provider=custom_llm_provider)
        assert kwargs["model"] == expected_model
        assert kwargs["custom_llm_provider"] == expected_provider

    @pytest.mark.parametrize(
        ("model", "routed_model"),
        [
            ("openrouter/auto", "openrouter/openrouter/auto"),
            ("openrouter/free", "openrouter/openrouter/free"),
            ("openrouter/fusion", "openrouter/openrouter/fusion"),
            ("openrouter/pareto-code", "openrouter/openrouter/pareto-code"),
        ],
    )
    def test_router_model_preserves_openrouter_slug(self, model, routed_model):
        resolved_model, provider, _, _ = get_llm_provider(routed_model)
        assert resolved_model == model
        assert provider == "openrouter"
        explicit_model, explicit_provider, _, _ = get_llm_provider(
            routed_model,
            custom_llm_provider="openrouter",
        )
        assert explicit_model == model
        assert explicit_provider == "openrouter"
        request = OpenrouterConfig().transform_request(
            model=resolved_model,
            messages=[{"role": "user", "content": "test"}],
            optional_params={},
            litellm_params={},
            headers={},
        )
        assert request["model"] == model

    @pytest.mark.asyncio
    async def test_non_openrouter_model_unaffected(self, monkeypatch):
        kwargs = await _run(monkeypatch, "gpt-4o", {
            "provider_only": ["z-ai"],
            "max_tokens": 16000,
        })
        assert "extra_body" not in kwargs
        assert "max_tokens" not in kwargs

    @pytest.mark.asyncio
    async def test_invalid_reasoning_effort_ignored(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {"reasoning_effort": "loww"})
        assert "extra_body" not in kwargs

    @pytest.mark.asyncio
    async def test_reasoning_none_overrides_budget(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {
            "reasoning_effort": "none",
            "reasoning_max_tokens": 2048,
        })
        assert kwargs["extra_body"]["reasoning"] == {"enabled": False}

    @pytest.mark.asyncio
    async def test_reasoning_budget_overrides_global_none(self, monkeypatch):
        logger = MagicMock()
        monkeypatch.setattr(litellm_handler, "get_logger", lambda: logger)
        kwargs = await _run(
            monkeypatch,
            "openrouter/google/gemini-2.5-pro",
            {"reasoning_max_tokens": 2048},
            reasoning_effort="none",
        )
        assert kwargs["extra_body"]["reasoning"] == {"max_tokens": 2048}
        assert any(
            "Ignoring config.reasoning_effort='none'" in call.args[0]
            for call in logger.warning.call_args_list
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "model",
        [
            "openrouter/google/gemini-2.5-pro",
            "openrouter/google/gemini-2.5-pro:nitro",
            "openrouter/google/gemini-2.5-pro:floor",
            "openrouter/google/gemini-2.5-flash",
        ],
    )
    async def test_global_reasoning_effort_uses_openrouter_body(self, monkeypatch, model):
        kwargs = await _run(
            monkeypatch,
            model,
            {},
            reasoning_effort="low",
        )
        assert "reasoning_effort" not in kwargs
        assert kwargs["extra_body"]["reasoning"] == {"effort": "low"}
        assert kwargs["model"] == model

    @pytest.mark.asyncio
    async def test_openrouter_effort_overrides_global_effort(self, monkeypatch):
        kwargs = await _run(
            monkeypatch,
            "openrouter/google/gemini-2.5-pro",
            {"reasoning_effort": "high"},
            reasoning_effort="low",
        )
        assert kwargs["extra_body"]["reasoning"] == {"effort": "high"}

    @pytest.mark.asyncio
    async def test_invalid_openrouter_effort_falls_back_to_global_effort(self, monkeypatch):
        kwargs = await _run(
            monkeypatch,
            "openrouter/google/gemini-2.5-pro",
            {"reasoning_effort": "hgh"},
            reasoning_effort="high",
        )
        assert kwargs["extra_body"]["reasoning"] == {"effort": "high"}

    @pytest.mark.asyncio
    async def test_registered_model_inherits_default_global_effort(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/google/gemini-2.5-pro", {})
        assert kwargs["extra_body"]["reasoning"] == {"effort": "medium"}

    @pytest.mark.asyncio
    async def test_global_none_disables_reasoning(self, monkeypatch):
        kwargs = await _run(
            monkeypatch,
            "openrouter/google/gemini-2.5-flash",
            {},
            reasoning_effort="none",
        )
        assert kwargs["extra_body"]["reasoning"] == {"enabled": False}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("global_effort", "openrouter", "expected"),
        [
            ("max", {}, "xhigh"),
            ("medium", {"reasoning_effort": "max"}, "xhigh"),
            ("minimal", {}, "minimal"),
        ],
    )
    async def test_openrouter_effort_normalization(
        self, monkeypatch, global_effort, openrouter, expected
    ):
        kwargs = await _run(
            monkeypatch,
            "openrouter/google/gemini-2.5-pro",
            openrouter,
            reasoning_effort=global_effort,
        )
        assert kwargs["extra_body"]["reasoning"] == {"effort": expected}

    @pytest.mark.asyncio
    async def test_reasoning_budget_suppresses_global_effort(self, monkeypatch):
        kwargs = await _run(
            monkeypatch,
            "openrouter/google/gemini-2.5-pro",
            {"reasoning_max_tokens": 2048},
            reasoning_effort="high",
        )
        assert kwargs["extra_body"]["reasoning"] == {"max_tokens": 2048}

    def test_litellm_requires_openrouter_reasoning_in_extra_body(self):
        """Pin the LiteLLM 1.98.0 workaround boundary so upgrades expose when it can be removed."""
        with pytest.raises(litellm.UnsupportedParamsError):
            get_optional_params(
                model="google/gemini-2.5-pro",
                custom_llm_provider="openrouter",
                reasoning_effort="low",
            )

        params = get_optional_params(
            model="google/gemini-2.5-pro",
            custom_llm_provider="openrouter",
            extra_body={"reasoning": {"effort": "low"}},
        )
        assert params["extra_body"]["reasoning"] == {"effort": "low"}

        disabled_params = get_optional_params(
            model="google/gemini-2.5-flash",
            custom_llm_provider="openrouter",
            extra_body={"reasoning": {"enabled": False}},
        )
        assert disabled_params["extra_body"]["reasoning"] == {"enabled": False}

    @pytest.mark.asyncio
    async def test_anthropic_reasoning_budget_warns_without_output_headroom(self, monkeypatch):
        logger = MagicMock()
        monkeypatch.setattr(litellm_handler, "get_logger", lambda: logger)
        kwargs = await _run(
            monkeypatch,
            "openrouter/anthropic/claude-3.7-sonnet",
            {"reasoning_max_tokens": 2048, "max_tokens": 1024},
        )
        assert kwargs["extra_body"]["reasoning"] == {"max_tokens": 2048}
        assert kwargs["max_tokens"] == 1024
        assert any(
            "must be greater than reasoning_max_tokens" in call.args[0]
            for call in logger.warning.call_args_list
        )

    @pytest.mark.asyncio
    async def test_anthropic_disabled_reasoning_skips_headroom_warning(self, monkeypatch):
        logger = MagicMock()
        monkeypatch.setattr(litellm_handler, "get_logger", lambda: logger)
        kwargs = await _run(
            monkeypatch,
            "openrouter/anthropic/claude-3.7-sonnet",
            {"reasoning_effort": "none", "reasoning_max_tokens": 2048, "max_tokens": 1024},
        )
        assert kwargs["extra_body"]["reasoning"] == {"enabled": False}
        assert not any(
            "must be greater than reasoning_max_tokens" in call.args[0]
            for call in logger.warning.call_args_list
        )

    @pytest.mark.asyncio
    async def test_string_overrides_are_coerced(self, monkeypatch):
        # Dynaconf/env overrides can arrive as strings; they must not crash or
        # be split into characters.
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {
            "provider_only": "z-ai",
            "max_tokens": "16000",
        })
        assert kwargs["extra_body"]["provider"] == {"only": ["z-ai"]}
        assert kwargs["max_tokens"] == 16000

    @pytest.mark.asyncio
    async def test_allow_fallbacks_string_false(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {
            "provider_order": ["z-ai", "novita"],
            "allow_fallbacks": "false",
        })
        assert kwargs["extra_body"]["provider"]["allow_fallbacks"] is False

    @pytest.mark.asyncio
    async def test_non_numeric_max_tokens_ignored(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {"max_tokens": "16k"})
        assert "max_tokens" not in kwargs

    @pytest.mark.asyncio
    async def test_azure_mode_does_not_mask_openrouter(self, monkeypatch):
        # Azure mode must not rewrite "openrouter/..." to "azure/openrouter/...":
        # that would misroute the request and skip the OpenRouter controls block.
        monkeypatch.setattr(litellm_handler, "get_settings",
                            lambda: _make_settings({"provider_only": ["z-ai"]}))
        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
                   new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _mock_response()
            handler = litellm_handler.LiteLLMAIHandler()
            handler.azure = True
            await handler.chat_completion(model="openrouter/z-ai/glm-5.2", system="sys", user="usr")
        kwargs = mock_call.call_args[1]
        assert kwargs["model"] == "openrouter/z-ai/glm-5.2"
        assert kwargs["extra_body"]["provider"] == {"only": ["z-ai"]}
