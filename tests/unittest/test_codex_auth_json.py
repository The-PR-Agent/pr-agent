import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from pr_agent.algo.ai_handlers.codex_auth import (
    CODEX_DEFAULT_API_BASE,
    apply_codex_auth_to_kwargs,
    load_codex_auth_from_settings,
)
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler


def _make_settings(**codex_values):
    codex = type("Codex", (), codex_values)()

    def get(_self, key, default=None):
        mapping = {
            "CODEX.AUTH_JSON": getattr(codex, "auth_json", None),
            "CODEX.AUTH_JSON_PATH": getattr(codex, "auth_json_path", None),
            "CODEX.API_BASE": getattr(codex, "api_base", None),
        }
        return mapping.get(key, default) or default

    return type("Settings", (), {"codex": codex, "get": get})()


def _make_litellm_settings(auth_json):
    class Config:
        reasoning_effort = "medium"
        ai_timeout = 30
        custom_reasoning_model = False
        max_model_tokens = 32000
        verbosity_level = 0
        seed = -1

        def get(self, _key, default=None):
            return default

    class LiteLLM:
        def get(self, _key, default=None):
            return default

    def get(_self, key, default=None):
        mapping = {
            "CODEX.AUTH_JSON": auth_json,
            "CODEX.AUTH_JSON_PATH": None,
            "CODEX.API_BASE": None,
        }
        return mapping.get(key, default) or default

    return type("Settings", (), {
        "config": Config(),
        "litellm": LiteLLM(),
        "get": get,
    })()


def _mock_response():
    mock = MagicMock()
    mock.__getitem__ = lambda self, key: {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]
    }[key]
    mock.dict.return_value = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    return mock


def test_load_codex_auth_from_inline_auth_json_tokens():
    auth_json = json.dumps({
        "tokens": {
            "access_token": "test-access-token",
            "refresh_token": "test-refresh-token",
            "id_token": "test-id-token",
            "account_id": "account-123",
        },
        "last_refresh": "2099-01-01T00:00:00Z",
    })

    auth = load_codex_auth_from_settings(_make_settings(auth_json=auth_json))

    assert auth.access_token == "test-access-token"
    assert auth.account_id == "account-123"
    assert auth.api_key is None
    assert auth.api_base == CODEX_DEFAULT_API_BASE


def test_apply_codex_auth_to_kwargs_adds_bearer_and_account_headers():
    auth_json = json.dumps({
        "tokens": {
            "access_token": "test-access-token",
            "refresh_token": "test-refresh-token",
            "id_token": "test-id-token",
            "account_id": "account-123",
        }
    })
    kwargs = {"model": "openai/gpt-5.1-codex", "extra_headers": {"x-existing": "kept"}}

    apply_codex_auth_to_kwargs(kwargs, _make_settings(auth_json=auth_json))

    assert kwargs["api_key"] == "test-access-token"
    assert kwargs["api_base"] == CODEX_DEFAULT_API_BASE
    assert kwargs["extra_headers"] == {
        "x-existing": "kept",
        "ChatGPT-Account-ID": "account-123",
    }


def test_apply_codex_auth_to_kwargs_skips_non_openai_provider_models():
    auth_json = json.dumps({"tokens": {"access_token": "test-access-token", "account_id": "account-123"}})
    kwargs = {"model": "databricks/databricks-claude-sonnet-4"}

    apply_codex_auth_to_kwargs(kwargs, _make_settings(auth_json=auth_json))

    assert "api_key" not in kwargs
    assert "api_base" not in kwargs


def test_codex_auth_json_openai_api_key_mode_uses_api_key_without_account_header():
    auth_json = json.dumps({"OPENAI_API_KEY": "sk-test-api-key"})
    kwargs = {"model": "openai/gpt-5.1-codex"}

    apply_codex_auth_to_kwargs(kwargs, _make_settings(auth_json=auth_json, api_base="https://example.test/v1"))

    assert kwargs["api_key"] == "sk-test-api-key"
    assert kwargs["api_base"] == "https://example.test/v1"
    assert "extra_headers" not in kwargs


def test_codex_auth_json_openai_api_key_mode_does_not_default_api_base():
    auth_json = json.dumps({"OPENAI_API_KEY": "sk-test-api-key"})
    kwargs = {"model": "openai/gpt-5.1-codex"}

    apply_codex_auth_to_kwargs(kwargs, _make_settings(auth_json=auth_json))

    assert kwargs["api_key"] == "sk-test-api-key"
    assert "api_base" not in kwargs


def test_codex_auth_json_path_is_read_without_logging_or_rewriting(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({
            "tokens": {
                "access_token": "path-access-token",
                "refresh_token": "path-refresh-token",
                "id_token": "path-id-token",
                "account_id": "account-from-path",
            }
        }),
        encoding="utf-8",
    )

    auth = load_codex_auth_from_settings(_make_settings(auth_json_path=str(auth_path)))

    assert auth.access_token == "path-access-token"
    assert auth.account_id == "account-from-path"
    assert json.loads(auth_path.read_text(encoding="utf-8"))["tokens"]["refresh_token"] == "path-refresh-token"


def test_codex_auth_json_requires_supported_credentials():
    with pytest.raises(ValueError, match="must contain either OPENAI_API_KEY or tokens.access_token"):
        load_codex_auth_from_settings(_make_settings(auth_json=json.dumps({"tokens": {}})))


@pytest.mark.asyncio
async def test_litellm_handler_applies_codex_auth_json_to_openai_compatible_call(monkeypatch):
    auth_json = json.dumps({"tokens": {"access_token": "test-access-token", "account_id": "account-123"}})
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_litellm_settings(auth_json))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = LiteLLMAIHandler()

        await handler.chat_completion(model="gpt-5.1-codex", system="sys", user="usr")

    forwarded = mock_call.call_args[1]
    assert forwarded["model"] == "openai/gpt-5.1-codex"
    assert forwarded["api_key"] == "test-access-token"
    assert forwarded["api_base"] == CODEX_DEFAULT_API_BASE
    assert forwarded["extra_headers"]["ChatGPT-Account-ID"] == "account-123"
