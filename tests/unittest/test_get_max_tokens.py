import litellm
import pytest

import pr_agent.algo.utils as utils
from pr_agent.algo import (
    _CLAUDE_MODEL_FAMILIES,
    CLAUDE_EXTENDED_THINKING_MODELS,
    NO_SUPPORT_TEMPERATURE_MODELS,
    _generate_claude_registries,
    _validate_claude_model_family,
)
from pr_agent.algo.utils import MAX_TOKENS, get_max_tokens


class TestGetMaxTokens:

    # Test if the file is in MAX_TOKENS
    def test_model_max_tokens(self, monkeypatch):
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        model = "gpt-3.5-turbo"
        expected = MAX_TOKENS[model]

        assert get_max_tokens(model) == expected

    @pytest.mark.parametrize("model", ["gpt-5.4", "gpt-5.4-2026-03-05"])
    def test_gpt54_model_max_tokens(self, monkeypatch, model):
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == 272000

    @pytest.mark.parametrize("model", ["gpt-5.4-mini", "gpt-5.4-mini-2026-03-17"])
    def test_gpt54_mini_model_max_tokens(self, monkeypatch, model):
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == 400000

    @pytest.mark.parametrize("model", ["gpt-5.4-nano", "gpt-5.4-nano-2026-03-17"])
    def test_gpt54_nano_model_max_tokens(self, monkeypatch, model):
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == 400000

    @pytest.mark.parametrize("model", ["gpt-5.5", "gpt-5.5-2026-04-23"])
    def test_gpt55_model_max_tokens(self, monkeypatch, model):
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == 1050000

    @pytest.mark.parametrize("model", ["gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"])
    def test_gpt56_model_max_tokens(self, monkeypatch, model):
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == 1050000

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("openrouter/auto", 2000000),
            ("openrouter/free", 200000),
            ("openrouter/fusion", 1000000),
            ("openrouter/pareto-code", 2000000),
        ],
    )
    def test_openrouter_router_model_max_tokens(self, monkeypatch, model, expected):
        fake_settings = type("", (), {
            "config": type("", (), {
                "custom_model_max_tokens": 0,
                "max_model_tokens": 0,
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == expected

    # Test situations where the model is not registered and exists as a custom model
    def test_model_has_custom(self, monkeypatch):
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 5000,
                'max_model_tokens': 0  # 제한 없음
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        model = "custom-model"
        expected = 5000

        assert get_max_tokens(model) == expected

    @pytest.mark.parametrize("model", [
        "gpt-5.1-codex",
        "gpt-5.2-codex",
        "gpt-5.3-codex",
    ])
    def test_gpt_codex_models_max_tokens(self, monkeypatch, model):
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        expected = MAX_TOKENS[model]

        assert get_max_tokens(model) == expected

    def test_model_not_max_tokens_and_not_has_custom(self, monkeypatch):
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        model = "custom-model"

        with pytest.raises(Exception):
            get_max_tokens(model)

    def test_model_max_tokens_with__limit(self, monkeypatch):
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 10000
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        model = "gpt-3.5-turbo"  # this model setting is 160000
        expected = 10000

        assert get_max_tokens(model) == expected

    @pytest.mark.parametrize("model", [
        "gemini/gemini-3-flash-preview",
        "vertex_ai/gemini-3-flash-preview",
        "gemini/gemini-3-pro-preview",
        "vertex_ai/gemini-3-pro-preview",
        "gemini/gemini-3.1-pro-preview",
        "vertex_ai/gemini-3.1-pro-preview",
        "gemini/gemini-3.1-flash",
        "vertex_ai/gemini-3.1-flash",
        "gemini/gemini-3.1-pro",
        "vertex_ai/gemini-3.1-pro",
        "gemini/gemini-3.1-flash-lite-preview",
        "vertex_ai/gemini-3.1-flash-lite-preview",
        "gemini/gemini-3.5-flash",
        "vertex_ai/gemini-3.5-flash",
        "gemini/gemini-3.5-flash-lite",
        "vertex_ai/gemini-3.5-flash-lite",
        "gemini/gemini-3.5-pro",
        "vertex_ai/gemini-3.5-pro",
        "gemini/gemini-3.6-flash",
        "vertex_ai/gemini-3.6-flash",
        "gemini/gemini-3.7-flash",
        "vertex_ai/gemini-3.7-flash",
        "gemini/gemini-3.8-flash",
        "vertex_ai/gemini-3.8-flash",
    ])
    def test_gemini_3_x_models_max_tokens(self, monkeypatch, model):
        fake_settings = type("", (), {
            "config": type("", (), {
                "custom_model_max_tokens": 0,
                "max_model_tokens": 0,
            })()
        })()
        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)
        assert get_max_tokens(model) == 1048576

    def test_bedrock_mantle_grok_4_3_model_max_tokens(self, monkeypatch):
        fake_settings = type("", (), {
            "config": type("", (), {
                "custom_model_max_tokens": 0,
                "max_model_tokens": 0,
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens("bedrock_mantle/xai.grok-4.3") == 1000000

    @pytest.mark.parametrize("model", [
        "xai/grok-4.5",
        "xai/grok-4.5-latest",
        "xai/grok-build-latest",
        "xai/grok-4.6",
        "openrouter/x-ai/grok-4.5",
        "openrouter/x-ai/grok-4.6",
    ])
    def test_xai_and_openrouter_grok_4_5_and_4_6_models_max_tokens(self, monkeypatch, model):
        fake_settings = type("", (), {
            "config": type("", (), {
                "custom_model_max_tokens": 0,
                "max_model_tokens": 0,
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == 500000

    @pytest.mark.parametrize(
        "model",
        [
            "anthropic/claude-opus-4-8",
            "claude-opus-4-8",
            "vertex_ai/claude-opus-4-8",
            "bedrock/anthropic.claude-opus-4-8",
            "bedrock/global.anthropic.claude-opus-4-8",
            "bedrock/us.anthropic.claude-opus-4-8",
            "bedrock/eu.anthropic.claude-opus-4-8",
            "bedrock/au.anthropic.claude-opus-4-8",
            "bedrock/jp.anthropic.claude-opus-4-8",
        ],
    )
    def test_claude_opus_4_8_model_max_tokens(self, monkeypatch, model):
        fake_settings = type("", (), {
            "config": type("", (), {
                "custom_model_max_tokens": 0,
                "max_model_tokens": 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == 1000000

    @pytest.mark.parametrize(
        "model",
        [
            "anthropic/claude-opus-5",
            "claude-opus-5",
            "vertex_ai/claude-opus-5",
            "bedrock/anthropic.claude-opus-5",
            "bedrock/global.anthropic.claude-opus-5",
            "bedrock/us.anthropic.claude-opus-5",
            "bedrock/eu.anthropic.claude-opus-5",
            "bedrock/au.anthropic.claude-opus-5",
            "bedrock/jp.anthropic.claude-opus-5",
        ],
    )
    def test_claude_opus_5_model_max_tokens(self, monkeypatch, model):
        fake_settings = type("", (), {
            "config": type("", (), {
                "custom_model_max_tokens": 0,
                "max_model_tokens": 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == 1000000

    @pytest.mark.parametrize(
        "model",
        [
            "anthropic/claude-opus-4-7",
            "claude-opus-4-7",
            "vertex_ai/claude-opus-4-7",
            "bedrock/anthropic.claude-opus-4-7",
            "bedrock/global.anthropic.claude-opus-4-7",
            "bedrock/us.anthropic.claude-opus-4-7",
        ],
    )
    def test_claude_opus_4_7_model_max_tokens(self, monkeypatch, model):
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == 1000000

    @pytest.mark.parametrize(
        "model",
        [
            "anthropic/claude-opus-4-6",
            "claude-opus-4-6",
            "vertex_ai/claude-opus-4-6",
            "bedrock/anthropic.claude-opus-4-6-v1:0",
            "bedrock/global.anthropic.claude-opus-4-6-v1:0",
            "bedrock/us.anthropic.claude-opus-4-6-v1:0",
        ],
    )
    def test_claude_opus_4_6_model_max_tokens(self, monkeypatch, model):
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == 200000

    @pytest.mark.parametrize(
        "model",
        [
            "anthropic/claude-sonnet-5",
            "claude-sonnet-5",
            "vertex_ai/claude-sonnet-5",
            "bedrock/anthropic.claude-sonnet-5",
            "bedrock/global.anthropic.claude-sonnet-5",
            "bedrock/us.anthropic.claude-sonnet-5",
            "bedrock/au.anthropic.claude-sonnet-5",
            "bedrock/eu.anthropic.claude-sonnet-5",
            "bedrock/jp.anthropic.claude-sonnet-5",
        ],
    )
    def test_claude_sonnet_5_model_max_tokens(self, monkeypatch, model):
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == 1000000

    @pytest.mark.parametrize(
        "model",
        [
            "anthropic/claude-sonnet-4-6",
            "claude-sonnet-4-6",
            "vertex_ai/claude-sonnet-4-6",
            "bedrock/anthropic.claude-sonnet-4-6",
            "bedrock/global.anthropic.claude-sonnet-4-6",
            "bedrock/us.anthropic.claude-sonnet-4-6",
            "bedrock/au.anthropic.claude-sonnet-4-6",
            "bedrock/eu.anthropic.claude-sonnet-4-6",
            "bedrock/jp.anthropic.claude-sonnet-4-6",
        ],
    )
    def test_claude_sonnet_4_6_model_max_tokens(self, monkeypatch, model):
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == 200000

    @pytest.mark.parametrize(
        "model",
        [
            "zai/glm-5.2",
        ],
    )
    def test_zai_glm_5_2_model_max_tokens(self, monkeypatch, model):
        fake_settings = type("", (), {
            "config": type("", (), {
                "custom_model_max_tokens": 0,
                "max_model_tokens": 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == 200000

    @pytest.mark.parametrize(
        "model",
        [
            "moonshot/kimi-k3",
        ],
    )
    def test_moonshot_kimi_k3_model_max_tokens(self, monkeypatch, model):
        fake_settings = type("", (), {
            "config": type("", (), {
                "custom_model_max_tokens": 0,
                "max_model_tokens": 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == 262144

    @pytest.mark.parametrize(
        "model",
        [
            "dashscope/qwen3.8-max",
        ],
    )
    def test_qwen_3_8_model_max_tokens(self, monkeypatch, model):
        fake_settings = type("", (), {
            "config": type("", (), {
                "custom_model_max_tokens": 0,
                "max_model_tokens": 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == 1000000



    @pytest.mark.parametrize(
        "model",
        [
            "xiaomi_mimo/mimo-v2.5",
        ],
    )
    def test_xiaomi_mimo_v2_5_model_max_tokens(self, monkeypatch, model):
        fake_settings = type("", (), {
            "config": type("", (), {
                "custom_model_max_tokens": 0,
                "max_model_tokens": 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == 1048576

    @pytest.mark.parametrize(
        "model",
        [
            "xiaomi_mimo/mimo-v2.5-pro",
        ],
    )
    def test_xiaomi_mimo_v2_5_pro_model_max_tokens(self, monkeypatch, model):
        fake_settings = type("", (), {
            "config": type("", (), {
                "custom_model_max_tokens": 0,
                "max_model_tokens": 0
            })()
        })()

        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == 1048576

    # --- LiteLLM fallback tests (issue #2957) ---

    def test_max_tokens_takes_precedence_over_litellm(self, monkeypatch):
        """MAX_TOKENS lookup must not consult LiteLLM."""
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 0
            })()
        })()
        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        def fail_if_called(*args, **kwargs):
            raise AssertionError("litellm.get_model_info should not be called")

        monkeypatch.setattr(litellm, "get_model_info", fail_if_called)

        model = "gpt-3.5-turbo"
        assert get_max_tokens(model) == MAX_TOKENS[model]

    def test_custom_model_max_tokens_takes_precedence_over_litellm(self, monkeypatch):
        """Positive custom_model_max_tokens must not consult LiteLLM."""
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 7000,
                'max_model_tokens': 0
            })()
        })()
        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        def fail_if_called(*args, **kwargs):
            raise AssertionError("litellm.get_model_info should not be called")

        monkeypatch.setattr(litellm, "get_model_info", fail_if_called)

        assert get_max_tokens("fake-provider/fake-model-xyz") == 7000

    def test_litellm_fallback_returns_max_input_tokens(self, monkeypatch):
        """Unknown model resolved via litellm.get_model_info max_input_tokens."""
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 0
            })()
        })()
        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)
        monkeypatch.setattr(litellm, "get_model_info",
                            lambda model: {"max_input_tokens": 65536})

        assert get_max_tokens("fake-provider/fake-model-xyz") == 65536

    def test_litellm_fallback_uses_max_input_tokens_not_max_tokens(self, monkeypatch):
        """Must use max_input_tokens, not max_tokens from litellm metadata."""
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 0
            })()
        })()
        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)
        monkeypatch.setattr(litellm, "get_model_info", lambda model: {
            "max_input_tokens": 32000,
            "max_tokens": 99999,
        })

        assert get_max_tokens("fake-provider/fake-model-xyz") == 32000

    def test_litellm_fallback_failure_preserves_existing_error(self, monkeypatch):
        """When litellm also cannot resolve, the existing error path fires."""
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 0
            })()
        })()
        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        def raise_for_unknown(model):
            raise Exception("This model isn't mapped yet")

        monkeypatch.setattr(litellm, "get_model_info", raise_for_unknown)

        with pytest.raises(Exception, match="Ensure .* is defined in MAX_TOKENS"):
            get_max_tokens("fake-provider/fake-unknown-model")

    def test_litellm_fallback_respects_max_model_tokens_cap(self, monkeypatch):
        """max_model_tokens cap applies to LiteLLM-resolved values."""
        fake_settings = type('', (), {
            'config': type('', (), {
                'custom_model_max_tokens': 0,
                'max_model_tokens': 8000
            })()
        })()
        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)
        monkeypatch.setattr(litellm, "get_model_info",
                            lambda model: {"max_input_tokens": 65536})

        assert get_max_tokens("fake-provider/fake-model-xyz") == 8000

    def test_litellm_fallback_uses_real_registry(self, monkeypatch):
        """Unknown model resolved against the real installed LiteLLM model registry."""
        model = "cohere/command-r-plus"

        assert model not in MAX_TOKENS

        model_info = litellm.get_model_info(model)
        expected_max_input_tokens = model_info["max_input_tokens"]

        assert isinstance(expected_max_input_tokens, int)
        assert expected_max_input_tokens > 0

        fake_settings = type("", (), {
            "config": type("", (), {
                "custom_model_max_tokens": 0,
                "max_model_tokens": 0,
            })()
        })()
        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)

        assert get_max_tokens(model) == expected_max_input_tokens

    def test_claude_opus_4_8_family_expansion_and_capabilities(self):
        """Independent verification of Opus 4.8 generated aliases and capabilities."""
        expected_aliases = {
            "claude-opus-4-8",
            "anthropic/claude-opus-4-8",
            "vertex_ai/claude-opus-4-8",
            "bedrock/anthropic.claude-opus-4-8",
            "bedrock/global.anthropic.claude-opus-4-8",
            "bedrock/us.anthropic.claude-opus-4-8",
            "bedrock/eu.anthropic.claude-opus-4-8",
            "bedrock/au.anthropic.claude-opus-4-8",
            "bedrock/jp.anthropic.claude-opus-4-8",
        }

        # Exact set equality against filtered MAX_TOKENS
        actual_aliases = {k for k in MAX_TOKENS if "claude-opus-4-8" in k}
        assert actual_aliases == expected_aliases

        # Context window tokens
        for alias in expected_aliases:
            assert MAX_TOKENS[alias] == 1000000

        # Capability lists
        for alias in expected_aliases:
            assert alias in NO_SUPPORT_TEMPERATURE_MODELS
            assert alias not in CLAUDE_EXTENDED_THINKING_MODELS

        # Negative checks to ensure no over-generation
        assert "bedrock/apac.anthropic.claude-opus-4-8" not in MAX_TOKENS
        assert "bedrock/apac.anthropic.claude-opus-4-8" not in NO_SUPPORT_TEMPERATURE_MODELS

    def test_claude_opus_4_6_family_expansion_and_capabilities(self):
        """Independent verification of Opus 4.6 generated aliases and capabilities."""
        expected_max_tokens_aliases = {
            "claude-opus-4-6",
            "anthropic/claude-opus-4-6",
            "vertex_ai/claude-opus-4-6",
            "bedrock/anthropic.claude-opus-4-6-v1:0",
            "bedrock/global.anthropic.claude-opus-4-6-v1:0",
            "bedrock/eu.anthropic.claude-opus-4-6-v1:0",
            "bedrock/au.anthropic.claude-opus-4-6-v1:0",
            "bedrock/jp.anthropic.claude-opus-4-6-v1:0",
            "bedrock/apac.anthropic.claude-opus-4-6-v1:0",
            "bedrock/us.anthropic.claude-opus-4-6-v1:0",
            "claude-opus-4-6-20260120",
            "anthropic/claude-opus-4-6-20260120",
            "vertex_ai/claude-opus-4-6@20260120",
            "bedrock/anthropic.claude-opus-4-6-20260120-v1:0",
            "bedrock/us.anthropic.claude-opus-4-6-20260120-v1:0",
        }

        # Exact set equality against filtered MAX_TOKENS
        actual_aliases = {k for k in MAX_TOKENS if "claude-opus-4-6" in k}
        assert actual_aliases == expected_max_tokens_aliases

        # Context window tokens
        for alias in expected_max_tokens_aliases:
            assert MAX_TOKENS[alias] == 200000

        # Extended thinking: 9 aliases
        expected_thinking_aliases = {
            "anthropic/claude-opus-4-6",
            "claude-opus-4-6",
            "vertex_ai/claude-opus-4-6",
            "bedrock/anthropic.claude-opus-4-6-v1:0",
            "bedrock/us.anthropic.claude-opus-4-6-v1:0",
            "bedrock/au.anthropic.claude-opus-4-6-v1:0",
            "bedrock/eu.anthropic.claude-opus-4-6-v1:0",
            "bedrock/jp.anthropic.claude-opus-4-6-v1:0",
            "bedrock/global.anthropic.claude-opus-4-6-v1:0",
        }
        actual_thinking_aliases = {k for k in CLAUDE_EXTENDED_THINKING_MODELS if "claude-opus-4-6" in k}
        assert actual_thinking_aliases == expected_thinking_aliases

        # Negative checks: apac and dated variants NOT in extended thinking
        assert "bedrock/apac.anthropic.claude-opus-4-6-v1:0" not in CLAUDE_EXTENDED_THINKING_MODELS
        assert "claude-opus-4-6-20260120" not in CLAUDE_EXTENDED_THINKING_MODELS
        assert "anthropic/claude-opus-4-6-20260120" not in CLAUDE_EXTENDED_THINKING_MODELS
        assert "vertex_ai/claude-opus-4-6@20260120" not in CLAUDE_EXTENDED_THINKING_MODELS

        # Negative checks: no Opus 4.6 in NO_SUPPORT_TEMPERATURE_MODELS
        for alias in expected_max_tokens_aliases:
            assert alias not in NO_SUPPORT_TEMPERATURE_MODELS

    def test_claude_model_family_metadata_key_validation(self):
        """Regression test for validating Claude model family metadata keys.

        Proves:
        - all current shipped families validate successfully.
        - an unknown top-level key (e.g. 'bedrock_region') raises ValueError naming the key and model_id.
        - an unknown nested extra_aliases key (e.g. 'extended_thinkin') raises ValueError naming the key, alias, and model_id.
        - global family definitions are not mutated.
        """
        # All current shipped families must pass validation
        for fam in _CLAUDE_MODEL_FAMILIES:
            _validate_claude_model_family(fam)

        # 1. Copied family with unknown top-level key must raise ValueError
        bad_fam = dict(_CLAUDE_MODEL_FAMILIES[0])
        bad_fam["bedrock_region"] = ("global", "us")

        with pytest.raises(ValueError) as exc_info:
            _validate_claude_model_family(bad_fam)

        err_msg = str(exc_info.value)
        assert "bedrock_region" in err_msg
        assert bad_fam["model_id"] in err_msg

        # Generator must also reject the unknown top-level key when expanding
        with pytest.raises(ValueError) as exc_info_gen:
            _generate_claude_registries(families=[bad_fam])
        assert "bedrock_region" in str(exc_info_gen.value)
        assert bad_fam["model_id"] in str(exc_info_gen.value)

        # 2. Family with unknown nested extra_aliases key must raise ValueError
        bad_nested_fam = {
            "model_id": "claude-opus-4-6",
            "extra_aliases": {
                "some/alias": {
                    "max_tokens": 200000,
                    "extended_thinkin": True,
                }
            },
        }

        with pytest.raises(ValueError) as exc_info_nested:
            _validate_claude_model_family(bad_nested_fam)

        err_nested = str(exc_info_nested.value)
        assert "extended_thinkin" in err_nested
        assert "some/alias" in err_nested
        assert "claude-opus-4-6" in err_nested

        # Generator must also reject the unknown nested key
        with pytest.raises(ValueError) as exc_info_gen_nested:
            _generate_claude_registries(families=[bad_nested_fam])
        assert "extended_thinkin" in str(exc_info_gen_nested.value)
        assert "some/alias" in str(exc_info_gen_nested.value)
        assert "claude-opus-4-6" in str(exc_info_gen_nested.value)

        # Ensure global families list was not mutated
        assert "bedrock_region" not in _CLAUDE_MODEL_FAMILIES[0]

    def test_claude_exceptional_extra_alias_capabilities(self):
        """Prove exceptional extra-alias capability routing.

        Proves:
        - scalar int extra_aliases only populate token counts.
        - structured extra_aliases route to no_temperature and extended_thinking when flagged.
        - capability flags do not bleed to unrelated aliases.
        """
        synthetic_family = {
            "model_id": "test-claude-synthetic",
            "max_tokens": 500000,
            "bedrock": False,
            "vertex": False,
            "extra_aliases": {
                "test/extra-token-only": 500000,
                "test/extra-no-temp": {
                    "max_tokens": 500000,
                    "no_temperature": True,
                },
                "test/extra-thinking": {
                    "max_tokens": 500000,
                    "extended_thinking": True,
                },
            },
        }

        tokens, no_temp, thinking = _generate_claude_registries(families=[synthetic_family])

        # Token verification
        assert tokens["test-claude-synthetic"] == 500000
        assert tokens["anthropic/test-claude-synthetic"] == 500000
        assert tokens["test/extra-token-only"] == 500000
        assert tokens["test/extra-no-temp"] == 500000
        assert tokens["test/extra-thinking"] == 500000

        # No-temperature capability routing (no bleed)
        assert "test/extra-no-temp" in no_temp
        assert "test/extra-token-only" not in no_temp
        assert "test/extra-thinking" not in no_temp

        # Extended-thinking capability routing (no bleed)
        assert "test/extra-thinking" in thinking
        assert "test/extra-token-only" not in thinking
        assert "test/extra-no-temp" not in thinking

    def test_claude_registries_baseline_parity_and_no_duplicates(self):
        """Verify baseline capability parity and that no duplicate entries exist."""
        # Capability lists have no duplicates
        assert len(NO_SUPPORT_TEMPERATURE_MODELS) == len(set(NO_SUPPORT_TEMPERATURE_MODELS))
        assert len(CLAUDE_EXTENDED_THINKING_MODELS) == len(set(CLAUDE_EXTENDED_THINKING_MODELS))

        # Extended thinking and no-temperature for Claude models are disjoint
        claude_thinking = {m for m in CLAUDE_EXTENDED_THINKING_MODELS if "claude" in m}
        claude_no_temp = {m for m in NO_SUPPORT_TEMPERATURE_MODELS if "claude" in m}
        assert claude_thinking.isdisjoint(claude_no_temp)
