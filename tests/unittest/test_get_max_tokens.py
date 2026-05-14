import pytest

import pr_agent.algo.utils as utils
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
        "gemini/gemini-3.1-flash-lite-preview",
        "vertex_ai/gemini-3.1-flash-lite-preview",
    ])
    def test_gemini_3_and_3_1_pro_preview(self, monkeypatch, model):
        fake_settings = type("", (), {
            "config": type("", (), {
                "custom_model_max_tokens": 0,
                "max_model_tokens": 0,
            })()
        })()
        monkeypatch.setattr(utils, "get_settings", lambda: fake_settings)
        assert get_max_tokens(model) == 1048576

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
