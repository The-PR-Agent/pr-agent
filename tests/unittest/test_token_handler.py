import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pr_agent.algo import token_handler


def _settings(model="primary-model", estimate_factor=0, openai_key=None, anthropic_key=None,
              gemini_key=None, ai_timeout=None, azure_api_type=None, azure_api_base=None,
              deployment_id=None):
    return SimpleNamespace(
        config=SimpleNamespace(model=model),
        get=lambda key, default=None: {
            "OPENAI.KEY": openai_key,
            "ANTHROPIC.KEY": anthropic_key,
            "GOOGLE_AI_STUDIO.GEMINI_API_KEY": gemini_key,
            "OPENAI.API_TYPE": azure_api_type,
            "OPENAI.API_BASE": azure_api_base,
            "openai.deployment_id": deployment_id,
            "config.model_token_count_estimate_factor": estimate_factor,
            "config.ai_timeout": ai_timeout,
        }.get(key, default),
    )


def _patch_acount_tokens(monkeypatch, acount_tokens=None):
    import litellm

    mock = acount_tokens or AsyncMock()
    monkeypatch.setattr(litellm, "acount_tokens", mock)
    return mock


def _handler(model, tokens=10):
    handler = token_handler.TokenHandler.__new__(token_handler.TokenHandler)
    handler.model = model
    handler.encoder = MagicMock()
    handler.encoder.encode.return_value = [0] * tokens
    return handler


def test_oversized_claude_patch_falls_back_to_local_estimate(monkeypatch):
    settings = _settings(
        model="claude-sonnet-4-6",
        estimate_factor=0.3,
        anthropic_key="test-key",
    )
    monkeypatch.setattr(
        token_handler, "get_settings", lambda use_context=False: settings
    )
    mock = _patch_acount_tokens(monkeypatch)

    handler = _handler("claude-sonnet-4-6")
    handler.CLAUDE_MAX_CONTENT_SIZE = 3

    assert handler.count_tokens("abcd", force_accurate=True) == 13
    mock.assert_not_called()


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


def test_force_accurate_openai_uses_litellm_acount_tokens(monkeypatch):
    settings = _settings(
        model="claude-sonnet-4-6",
        estimate_factor=0.3,
        openai_key="openai-settings-key",
    )
    monkeypatch.setattr(token_handler, "get_settings", lambda use_context=True: settings)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    mock = _patch_acount_tokens(
        monkeypatch,
        acount_tokens=AsyncMock(
            return_value=SimpleNamespace(tokenizer_type="openai_api", total_tokens=42)
        ),
    )
    handler = _handler("gpt-4o")

    assert handler.count_tokens("patch", force_accurate=True) == 42
    mock.assert_awaited_once_with(
        model="gpt-4o",
        messages=[{"role": "user", "content": "patch"}],
        system="system",
        api_key="openai-settings-key",
        api_base=None,
    )


def test_force_accurate_routes_bare_azure_model_without_deployment(monkeypatch):
    settings = _settings(
        model="gpt-4o",
        estimate_factor=0.3,
        openai_key="azure-settings-key",
        azure_api_type="azure",
        azure_api_base="https://acme.openai.azure.com/",
    )
    monkeypatch.setattr(token_handler, "get_settings", lambda use_context=False: settings)
    monkeypatch.delenv("AZURE_API_BASE", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    mock = _patch_acount_tokens(
        monkeypatch,
        acount_tokens=AsyncMock(
            return_value=SimpleNamespace(tokenizer_type="azure_tokenizer", total_tokens=42)
        ),
    )
    handler = _handler("gpt-4o")

    assert handler.count_tokens("patch", force_accurate=True) == 42
    mock.assert_awaited_once_with(
        model="azure/gpt-4o",
        messages=[{"role": "user", "content": "patch"}],
        system="system",
        api_key="azure-settings-key",
        api_base="https://acme.openai.azure.com/",
    )


def test_force_accurate_routes_azure_deployment_id(monkeypatch):
    settings = _settings(
        model="gpt-4o",
        estimate_factor=0.3,
        openai_key="azure-settings-key",
        azure_api_type="azure",
        azure_api_base="https://acme.openai.azure.com/",
        deployment_id="my-deployment",
    )
    monkeypatch.setattr(token_handler, "get_settings", lambda use_context=False: settings)
    monkeypatch.delenv("AZURE_API_BASE", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    mock = _patch_acount_tokens(
        monkeypatch,
        acount_tokens=AsyncMock(
            return_value=SimpleNamespace(tokenizer_type="azure_tokenizer", total_tokens=42)
        ),
    )
    handler = _handler("gpt-4o")

    assert handler.count_tokens("patch", force_accurate=True) == 42
    mock.assert_awaited_once_with(
        model="azure/my-deployment",
        messages=[{"role": "user", "content": "patch"}],
        system="system",
        api_key="azure-settings-key",
        api_base="https://acme.openai.azure.com/",
    )


def test_force_accurate_uses_litellm_acount_tokens_for_configured_model(monkeypatch):
    settings = _settings(
        model="claude-opus-4-8",
        estimate_factor=0.3,
        anthropic_key="anthropic-settings-key",
    )
    monkeypatch.setattr(token_handler, "get_settings", lambda use_context=True: settings)
    mock = _patch_acount_tokens(
        monkeypatch,
        acount_tokens=AsyncMock(
            return_value=SimpleNamespace(tokenizer_type="anthropic_api", total_tokens=99)
        ),
    )
    handler = _handler("claude-opus-4-8")

    assert handler.count_tokens("patch", force_accurate=True) == 99
    mock.assert_awaited_once_with(
        model="claude-opus-4-8",
        messages=[{"role": "user", "content": "patch"}],
        system="system",
        api_key="anthropic-settings-key",
        api_base=None,
    )


def test_force_accurate_routes_non_claude_provider_to_acount_tokens(monkeypatch):
    settings = _settings(
        model="gemini/gemini-2.5-pro",
        estimate_factor=0.3,
        gemini_key="gemini-settings-key",
    )
    monkeypatch.setattr(token_handler, "get_settings", lambda use_context=True: settings)
    mock = _patch_acount_tokens(
        monkeypatch,
        acount_tokens=AsyncMock(
            return_value=SimpleNamespace(tokenizer_type="gemini_tokenizer", total_tokens=77)
        ),
    )
    handler = _handler("gemini/gemini-2.5-pro")

    assert handler.count_tokens("patch", force_accurate=True) == 77
    mock.assert_awaited_once_with(
        model="gemini/gemini-2.5-pro",
        messages=[{"role": "user", "content": "patch"}],
        system="system",
        api_key="gemini-settings-key",
        api_base=None,
    )


def test_force_accurate_local_tokenizer_estimate_applies_factor(monkeypatch):
    settings = _settings(
        model="claude-opus-4-8",
        estimate_factor=0.3,
        anthropic_key="test-key",
    )
    monkeypatch.setattr(token_handler, "get_settings", lambda use_context=True: settings)
    _patch_acount_tokens(
        monkeypatch,
        acount_tokens=AsyncMock(
            return_value=SimpleNamespace(tokenizer_type="local_tokenizer", total_tokens=999)
        ),
    )
    handler = _handler("claude-opus-4-8")

    assert handler.count_tokens("patch", force_accurate=True) == 13


def test_force_accurate_acount_tokens_error_falls_back_to_factor(monkeypatch):
    settings = _settings(
        model="claude-opus-4-8",
        estimate_factor=0.3,
        anthropic_key="test-key",
    )
    monkeypatch.setattr(token_handler, "get_settings", lambda use_context=True: settings)
    mock = _patch_acount_tokens(monkeypatch, acount_tokens=AsyncMock(side_effect=RuntimeError("boom")))
    handler = _handler("claude-opus-4-8")

    assert handler.count_tokens("patch", force_accurate=True) == 13
    mock.assert_awaited_once()


def test_force_accurate_acount_tokens_timeout_falls_back_to_factor(monkeypatch):
    settings = _settings(
        model="claude-opus-4-8",
        estimate_factor=0.3,
        anthropic_key="test-key",
        ai_timeout=0.01,
    )
    monkeypatch.setattr(token_handler, "get_settings", lambda use_context=True: settings)

    async def _slow_count(**kwargs):
        await asyncio.sleep(1)
        return SimpleNamespace(tokenizer_type="anthropic_api", total_tokens=99)

    _patch_acount_tokens(monkeypatch, acount_tokens=_slow_count)
    handler = _handler("claude-opus-4-8")

    assert handler.count_tokens("patch", force_accurate=True) == 13


def test_await_coroutine_propagates_context_to_worker():
    import contextvars

    marker = contextvars.ContextVar("marker", default=None)

    async def _read_marker():
        return marker.get()

    async def _probe():
        marker.set("set-in-caller")
        return token_handler._await_coroutine(_read_marker())

    assert asyncio.run(_probe()) == "set-in-caller"
