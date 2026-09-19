import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

from pr_agent.algo import token_handler


def _settings(model="primary-model", estimate_factor=0, openai_key=None, anthropic_key=None):
    return SimpleNamespace(
        config=SimpleNamespace(model=model),
        get=lambda key, default=None: {
            "openai.key": openai_key,
            "anthropic.key": anthropic_key,
            "config.model_token_count_estimate_factor": estimate_factor,
        }.get(key, default),
    )


def test_oversized_claude_patch_falls_back_to_local_estimate(monkeypatch):
    settings = SimpleNamespace(
        config=SimpleNamespace(model="claude-3-7-sonnet-20250219"),
        get=lambda key, default=None: {
            "anthropic.key": "test-key",
            "config.model_token_count_estimate_factor": 0.3,
        }.get(key, default),
    )
    monkeypatch.setattr(
        token_handler, "get_settings", lambda use_context=False: settings
    )

    client = MagicMock()
    anthropic_module = types.ModuleType("anthropic")
    anthropic_module.Anthropic = MagicMock(return_value=client)
    monkeypatch.setitem(sys.modules, "anthropic", anthropic_module)

    handler = token_handler.TokenHandler.__new__(token_handler.TokenHandler)
    handler.encoder = MagicMock()
    handler.encoder.encode.return_value = [0] * 10
    handler.CLAUDE_MAX_CONTENT_SIZE = 3

    assert handler.count_tokens("abcd", force_accurate=True) == 13
    client.messages.count_tokens.assert_not_called()


def test_no_pr_handler_initializes_zero_prompt_tokens(monkeypatch):
    monkeypatch.setattr(token_handler, "get_settings", lambda use_context=True: _settings())
    monkeypatch.setattr(
        token_handler.TokenEncoder,
        "get_token_encoder",
        lambda _model=None: MagicMock(),
    )

    handler = token_handler.TokenHandler(model="fallback-model")

    assert handler.model == "fallback-model"
    assert handler.prompt_tokens == 0


def test_for_model_rebinds_rendered_prompt_without_mutating_source(monkeypatch):
    monkeypatch.setattr(token_handler, "get_settings", lambda use_context=True: _settings())
    encoders = {}

    class CharacterEncoder:
        def __init__(self, model):
            self.model = model

        @staticmethod
        def encode(text, disallowed_special=()):
            return list(text)

    def get_encoder(model=None):
        encoders.setdefault(model, CharacterEncoder(model))
        return encoders[model]

    monkeypatch.setattr(token_handler.TokenEncoder, "get_token_encoder", get_encoder)
    variables = {"title": "PR"}
    handler = token_handler.TokenHandler(object(), variables, "system {{ title }}", "user")

    fallback_handler = handler.for_model("fallback-model")

    assert handler.for_model("primary-model") is handler
    assert handler.model == "primary-model"
    assert fallback_handler is not handler
    assert fallback_handler.model == "fallback-model"
    assert fallback_handler.vars is variables
    assert fallback_handler.encoder.model == "fallback-model"
    assert fallback_handler.prompt_tokens == len("system PR") + len("user")


def test_for_model_does_not_replace_configured_primary_encoder_cache(monkeypatch):
    monkeypatch.setattr(token_handler, "get_settings", lambda use_context=True: _settings())
    monkeypatch.setattr(token_handler.TokenEncoder, "_encoder_instance", None)
    monkeypatch.setattr(token_handler.TokenEncoder, "_model", None)
    created_models = []

    def create_encoder(model):
        created_models.append(model)
        encoder = MagicMock()
        encoder.encode.return_value = []
        return encoder

    monkeypatch.setattr(
        token_handler.TokenEncoder,
        "_create_encoder",
        staticmethod(create_encoder),
    )
    handler = token_handler.TokenHandler(object(), {}, "system", "user")
    primary_encoder = token_handler.TokenEncoder._encoder_instance

    fallback_handler = handler.for_model("fallback-model")

    assert fallback_handler.encoder is not primary_encoder
    assert token_handler.TokenEncoder._encoder_instance is primary_encoder
    assert token_handler.TokenEncoder._model == "primary-model"
    assert created_models == ["primary-model", "fallback-model"]


def test_force_accurate_count_uses_bound_attempt_model(monkeypatch):
    settings = _settings(
        model="claude-3-7-sonnet-20250219",
        estimate_factor=0.3,
        openai_key="test-key",
        anthropic_key="test-key",
    )
    monkeypatch.setattr(token_handler, "get_settings", lambda use_context=True: settings)
    handler = token_handler.TokenHandler.__new__(token_handler.TokenHandler)
    handler.model = "gpt-4o"
    handler.encoder = MagicMock()
    handler.encoder.encode.return_value = [0] * 10
    handler._calc_claude_tokens = MagicMock(return_value=99)

    assert handler.count_tokens("patch", force_accurate=True) == 10
    handler._calc_claude_tokens.assert_not_called()
