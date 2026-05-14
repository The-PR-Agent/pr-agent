"""
Tests for the litellm.api_key guard in LiteLLMAIHandler.chat_completion.

Verifies:
  - Placeholder key (DUMMY_LITELLM_API_KEY) is never injected into the call.
  - None is not injected (e.g. when OpenAI key is set via litellm.openai_key).
  - Real provider keys (Groq, XAI, OpenRouter, Azure AD) ARE injected.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from pr_agent.algo.ai_handlers.litellm_ai_handler import DUMMY_LITELLM_API_KEY, LiteLLMAIHandler


def _make_settings():
    """Minimal settings object that satisfies __init__ and chat_completion."""
    return type("Settings", (), {
        "config": type("Config", (), {
            "reasoning_effort": None,
            "ai_timeout": 30,
            "custom_reasoning_model": False,
            "max_model_tokens": 32000,
            "verbosity_level": 0,
            "seed": -1,
            "get": lambda self, key, default=None: default,
        })(),
        "litellm": type("LiteLLM", (), {
            "get": lambda self, key, default=None: default,
        })(),
        "get": lambda self, key, default=None: default,
    })()


def _mock_response():
    """Minimal acompletion response."""
    mock = MagicMock()
    mock.__getitem__ = lambda self, key: {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]
    }[key]
    mock.dict.return_value = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    return mock


@pytest.fixture(autouse=True)
def patch_settings(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings())


def _make_anthropic_settings():
    """Settings with ANTHROPIC.KEY configured, no OPENAI.KEY.

    This simulates the original bug scenario: ANTHROPIC.KEY is set,
    but OPENAI.KEY is not, so litellm.api_key falls back to DUMMY_LITELLM_API_KEY.
    """
    anthropic_key = "test-anthropic-key-12345"
    return type("Settings", (), {
        "config": type("Config", (), {
            "reasoning_effort": None,
            "ai_timeout": 30,
            "custom_reasoning_model": False,
            "max_model_tokens": 32000,
            "verbosity_level": 0,
            "seed": -1,
            "get": lambda self, key, default=None: default,
        })(),
        "litellm": type("LiteLLM", (), {
            "get": lambda self, key, default=None: default,
        })(),
        "anthropic": type("Anthropic", (), {
            "key": anthropic_key
        })(),
        # Return the Anthropic key when settings.get("ANTHROPIC.KEY") is called
        "get": lambda self, key, default=None: (
            anthropic_key if key == "ANTHROPIC.KEY" else default
        ),
    })()


class TestApiKeyGuard:

    @pytest.mark.asyncio
    async def test_dummy_key_not_forwarded(self, monkeypatch):
        """api_key must NOT appear in kwargs when litellm.api_key is the placeholder."""
        monkeypatch.setattr(litellm, "api_key", DUMMY_LITELLM_API_KEY)

        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
                   new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _mock_response()
            handler = LiteLLMAIHandler()
            await handler.chat_completion(model="gpt-4o", system="sys", user="usr")

        assert "api_key" not in mock_call.call_args[1]

    @pytest.mark.asyncio
    async def test_none_api_key_not_forwarded(self, monkeypatch):
        """api_key must NOT appear in kwargs when litellm.api_key is None.

        This is the OpenAI-key path: OPENAI.KEY sets litellm.openai_key,
        leaving litellm.api_key at None.
        """
        monkeypatch.setattr(litellm, "api_key", None)

        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
                   new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _mock_response()
            handler = LiteLLMAIHandler()
            await handler.chat_completion(model="gpt-4o", system="sys", user="usr")

        assert "api_key" not in mock_call.call_args[1]

    @pytest.mark.asyncio
    async def test_real_key_forwarded(self, monkeypatch):
        """api_key IS injected when a real provider key is in litellm.api_key (e.g. Groq, XAI).

        The key is set after __init__ to simulate a provider having stored its key there
        during initialization, without triggering the placeholder value in __init__.
        """
        real_key = "test-provider-key-67890"

        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
                   new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _mock_response()
            handler = LiteLLMAIHandler()
            # Set after init so __init__'s own dummy-key assignment doesn't overwrite it
            monkeypatch.setattr(litellm, "api_key", real_key)
            await handler.chat_completion(model="gpt-4o", system="sys", user="usr")

        assert mock_call.call_args[1]["api_key"] == real_key

    @pytest.mark.asyncio
    async def test_anthropic_key_not_shadowed_by_dummy_key(self, monkeypatch):
        """Original bug scenario: ANTHROPIC.KEY configured without OPENAI.KEY.

        During __init__, litellm.api_key is set to DUMMY_LITELLM_API_KEY (fallback)
        because OPENAI.KEY is not configured. But litellm.anthropic_key is also set.
        The guard must prevent the dummy key from being passed to the call,
        allowing litellm to use anthropic_key internally.

        This test replicates the exact bug from GitHub issue #2042.
        """
        # Override settings to simulate Anthropic configured, OpenAI not configured
        monkeypatch.setattr(litellm_handler, "get_settings", _make_anthropic_settings)

        # Ensure deterministic preconditions: delete OPENAI_API_KEY env var so __init__
        # will set litellm.api_key to DUMMY_LITELLM_API_KEY (line 42-43 of litellm_ai_handler.py)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        # Reset litellm.api_key to avoid cross-test state pollution
        monkeypatch.setattr(litellm, "api_key", None)

        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
                   new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _mock_response()
            handler = LiteLLMAIHandler()

            # After init: litellm.api_key should be the dummy (OpenAI fallback),
            # but litellm.anthropic_key is the real Anthropic key
            assert litellm.api_key == DUMMY_LITELLM_API_KEY

            # Call with Anthropic model
            await handler.chat_completion(
                model="claude-3-5-sonnet-20241022",
                system="sys",
                user="usr"
            )

            # Verify the dummy key was NOT passed to the call.
            # This allows litellm to use litellm.anthropic_key internally.
            assert "api_key" not in mock_call.call_args[1]

    @pytest.mark.asyncio
    async def test_groq_key_forwarded_for_non_ollama_model(self, monkeypatch):
        """Regression check for PR #2288: Groq key must be forwarded for non-Ollama models.

        PR #2288 changed the forwarding guard to only forward api_key when
        model.startswith('ollama'). This test verifies whether that approach
        silently drops the Groq key when calling a non-Ollama model (e.g. gpt-4o).

        Groq sets litellm.api_key during __init__ (see litellm_ai_handler.py line 73)
        and relies on it being passed via kwargs["api_key"] to acompletion.
        """
        groq_key = "test-groq-key-12345"

        groq_settings = type("Settings", (), {
            "config": type("Config", (), {
                "reasoning_effort": None,
                "ai_timeout": 30,
                "custom_reasoning_model": False,
                "max_model_tokens": 32000,
                "verbosity_level": 0,
                "seed": -1,
                "get": lambda self, key, default=None: default,
            })(),
            "litellm": type("LiteLLM", (), {
                "get": lambda self, key, default=None: default,
            })(),
            "groq": type("Groq", (), {
                "key": groq_key,
            })(),
            "get": lambda self, key, default=None: (
                groq_key if key == "GROQ.KEY" else default
            ),
        })()

        monkeypatch.setattr(litellm_handler, "get_settings", lambda: groq_settings)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(litellm, "api_key", None)

        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
                   new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _mock_response()
            handler = LiteLLMAIHandler()

            # Confirm __init__ stored the Groq key in litellm.api_key
            assert litellm.api_key == groq_key, (
                f"Expected litellm.api_key to be Groq key after __init__, got: {litellm.api_key!r}"
            )

            # Call with a non-Ollama model
            await handler.chat_completion(model="gpt-4o", system="sys", user="usr")

        # The Groq key must be forwarded — without it, Groq calls will fail auth
        assert mock_call.call_args[1].get("api_key") == groq_key, (
            f"Groq key was NOT forwarded to acompletion. "
            f"kwargs had: {mock_call.call_args[1]}"
        )

    @pytest.mark.asyncio
    async def test_xai_key_forwarded_for_non_ollama_model(self, monkeypatch):
        """Regression check for PR #2288: xAI key must be forwarded for non-Ollama models.

        Similar to Groq, xAI sets litellm.api_key during __init__ and relies on
        it being forwarded via kwargs["api_key"]. PR #2288's model-scoped approach
        would also break xAI.
        """
        xai_key = "xai-test-key-67890"

        xai_settings = type("Settings", (), {
            "config": type("Config", (), {
                "reasoning_effort": None,
                "ai_timeout": 30,
                "custom_reasoning_model": False,
                "max_model_tokens": 32000,
                "verbosity_level": 0,
                "seed": -1,
                "get": lambda self, key, default=None: default,
            })(),
            "litellm": type("LiteLLM", (), {
                "get": lambda self, key, default=None: default,
            })(),
            "xai": type("XAI", (), {
                "key": xai_key,
            })(),
            "get": lambda self, key, default=None: (
                xai_key if key == "XAI.KEY" else default
            ),
        })()

        monkeypatch.setattr(litellm_handler, "get_settings", lambda: xai_settings)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(litellm, "api_key", None)

        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
                   new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _mock_response()
            handler = LiteLLMAIHandler()

            assert litellm.api_key == xai_key
            await handler.chat_completion(model="gpt-4o", system="sys", user="usr")

        assert mock_call.call_args[1].get("api_key") == xai_key

    @pytest.mark.asyncio
    async def test_ollama_and_groq_coexist(self, monkeypatch):
        """Verify both Ollama and Groq keys can coexist and be forwarded correctly.

        When multiple providers are configured, litellm.api_key gets overwritten
        sequentially during __init__. The sentinel guard should still forward
        whatever real key is currently in litellm.api_key.
        """
        groq_key = "gsk-groq-key"
        ollama_key = "ollama-key"

        # Simulate: Groq key set first, then Ollama overwrites litellm.api_key
        mixed_settings = type("Settings", (), {
            "config": type("Config", (), {
                "reasoning_effort": None,
                "ai_timeout": 30,
                "custom_reasoning_model": False,
                "max_model_tokens": 32000,
                "verbosity_level": 0,
                "seed": -1,
                "get": lambda self, key, default=None: default,
            })(),
            "litellm": type("LiteLLM", (), {
                "get": lambda self, key, default=None: default,
            })(),
            "groq": type("Groq", (), {"key": groq_key})(),
            "ollama": type("Ollama", (), {
                "api_key": ollama_key,
                "api_base": "http://localhost:11434",
            })(),
            "get": lambda self, key, default=None: (
                groq_key if key == "GROQ.KEY" else
                ollama_key if key == "OLLAMA.API_KEY" else
                "http://localhost:11434" if key == "OLLAMA.API_BASE" else
                default
            ),
        })()

        monkeypatch.setattr(litellm_handler, "get_settings", lambda: mixed_settings)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(litellm, "api_key", None)

        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
                   new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _mock_response()
            handler = LiteLLMAIHandler()

            # After init, litellm.api_key should be Ollama (last assignment)
            assert litellm.api_key == ollama_key

            # Call with Ollama model — should get Ollama key
            await handler.chat_completion(model="ollama/mistral", system="sys", user="usr")
            assert mock_call.call_args[1]["api_key"] == ollama_key

            # Call with non-Ollama model — should still forward the key
            # (which is Ollama in this case, but the guard correctly allows real keys through)
            await handler.chat_completion(model="gpt-4o", system="sys", user="usr")
            assert mock_call.call_args[1]["api_key"] == ollama_key
