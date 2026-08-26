"""
Tests for the litellm.api_key guard in LiteLLMAIHandler.chat_completion.

Verifies:
  - Placeholder key (DUMMY_LITELLM_API_KEY) is never injected into the call, and never
    occupies the global litellm.api_key (issue #2544).
  - None is not injected (e.g. when OpenAI key is set via litellm.openai_key).
  - Real provider keys (Groq, SambaNova, XAI, OpenRouter, Azure AD) ARE injected.
"""
import contextlib
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
    monkeypatch.setattr(litellm_handler, "get_settings", _make_settings)


@pytest.fixture(autouse=True)
def restore_litellm_globals():
    """Undo the LiteLLM globals LiteLLMAIHandler.__init__ writes.

    Constructing the handler mutates process-wide state (litellm.api_key and
    litellm.openai_key). monkeypatch only reverts what a test set itself, so without
    this the placeholder written during __init__ would outlive the test and make
    later tests order-dependent.
    """
    saved = {name: getattr(litellm, name) for name in ("api_key", "openai_key")}
    yield
    for name, value in saved.items():
        setattr(litellm, name, value)


def _make_anthropic_settings():
    """Settings with ANTHROPIC.KEY configured, no OPENAI.KEY.

    This simulates the original bug scenario: ANTHROPIC.KEY is set,
    but OPENAI.KEY is not, so __init__ falls back to the DUMMY_LITELLM_API_KEY placeholder.
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

        During __init__ the DUMMY_LITELLM_API_KEY placeholder is stored (as the OpenAI
        fallback) because OPENAI.KEY is not configured. But litellm.anthropic_key is also
        set. The guard must prevent the placeholder from being passed to the call,
        allowing litellm to use anthropic_key internally.

        This test replicates the exact bug from GitHub issue #2042.
        """
        # Override settings to simulate Anthropic configured, OpenAI not configured
        monkeypatch.setattr(litellm_handler, "get_settings", _make_anthropic_settings)

        # Ensure deterministic preconditions: delete OPENAI_API_KEY env var so __init__
        # takes the placeholder branch in litellm_ai_handler.py
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        # Reset litellm.api_key to avoid cross-test state pollution
        monkeypatch.setattr(litellm, "api_key", None)

        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
                   new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _mock_response()
            handler = LiteLLMAIHandler()

            # After init: the placeholder lives in litellm.openai_key (OpenAI fallback)
            # and never in litellm.api_key, which LiteLLM checks ahead of provider env
            # vars. litellm.anthropic_key holds the real Anthropic key.
            assert litellm.openai_key == DUMMY_LITELLM_API_KEY
            assert litellm.api_key != DUMMY_LITELLM_API_KEY

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
    async def test_sambanova_key_forwarded_for_non_ollama_model(self, monkeypatch):
        """SambaNova key must be forwarded for non-Ollama models.

        Like Groq and xAI, SambaNova sets litellm.api_key during __init__
        (see litellm_ai_handler.py) and relies on it being forwarded via
        kwargs["api_key"] to acompletion.
        """
        sambanova_key = "sambanova-test-key-67890"

        sambanova_settings = type("Settings", (), {
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
            "sambanova": type("SambaNova", (), {
                "key": sambanova_key,
            })(),
            "get": lambda self, key, default=None: (
                sambanova_key if key == "SAMBANOVA.KEY" else default
            ),
        })()

        monkeypatch.setattr(litellm_handler, "get_settings", lambda: sambanova_settings)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(litellm, "api_key", None)

        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
                   new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _mock_response()
            handler = LiteLLMAIHandler()

            assert litellm.api_key == sambanova_key
            await handler.chat_completion(
                model="sambanova/MiniMax-M3", system="sys", user="usr"
            )

        assert mock_call.call_args[1].get("api_key") == sambanova_key

    @pytest.mark.asyncio
    async def test_databricks_model_does_not_forward_foreign_key(self, monkeypatch):
        """Databricks models authenticate via DATABRICKS_API_KEY/DATABRICKS_API_BASE env vars.

        In a multi-provider config another provider (e.g. Groq/OpenRouter) may have stored
        its key in litellm.api_key during __init__. That key must NOT be forwarded for
        databricks/* calls, otherwise it would override the intended env-var auth and break
        Databricks authentication.
        """
        foreign_key = "test-groq-key-shadowing-databricks"

        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
                   new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _mock_response()
            handler = LiteLLMAIHandler()
            # Simulate another provider having populated litellm.api_key during init
            monkeypatch.setattr(litellm, "api_key", foreign_key)
            await handler.chat_completion(
                model="databricks/databricks-claude-sonnet-4", system="sys", user="usr"
            )

        assert "api_key" not in mock_call.call_args[1], (
            f"Foreign provider key must not be forwarded for databricks/* models. "
            f"kwargs had: {mock_call.call_args[1]}"
        )

    @pytest.mark.asyncio
    async def test_databricks_model_does_not_forward_foreign_api_base(self, monkeypatch):
        """Databricks models select their endpoint via the DATABRICKS_API_BASE env var.

        In a multi-provider config another provider (OpenRouter/Ollama/Azure AD/OpenAI) may
        have set self.api_base during __init__. That base URL must NOT be forwarded for
        databricks/* calls, otherwise it would route the request to the wrong host and
        override the intended DATABRICKS_API_BASE endpoint. The Databricks base (or None,
        which lets LiteLLM read the env var) must be used instead.
        """
        databricks_base = "https://adb-1234.azuredatabricks.net/serving-endpoints"
        monkeypatch.setenv("DATABRICKS_API_BASE", databricks_base)

        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
                   new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _mock_response()
            handler = LiteLLMAIHandler()
            # Simulate another provider having set api_base during init
            handler.api_base = "https://openrouter.ai/api/v1"
            await handler.chat_completion(
                model="databricks/databricks-claude-sonnet-4", system="sys", user="usr"
            )

        assert mock_call.call_args[1]["api_base"] == databricks_base, (
            f"Databricks endpoint must come from DATABRICKS_API_BASE, not a foreign provider's "
            f"api_base. kwargs had: {mock_call.call_args[1]}"
        )

    @pytest.mark.asyncio
    async def test_databricks_guards_survive_azure_mode(self, monkeypatch):
        """Azure mode must not rewrite databricks/* models and bypass the Databricks guards.

        When Azure is enabled in a multi-provider config (OPENAI.API_TYPE=azure or AZURE_AD),
        chat_completion() prepends 'azure/' to the model. If that rewrite happened for a
        databricks/* model the prefix-based guards would never trigger, routing the call to
        Azure with a foreign key/base. The model must keep its 'databricks/' prefix, the foreign
        key must not be forwarded, and api_base must come from DATABRICKS_API_BASE.
        """
        foreign_key = "test-azure-key-shadowing-databricks"
        databricks_base = "https://adb-1234.azuredatabricks.net/serving-endpoints"
        monkeypatch.setenv("DATABRICKS_API_BASE", databricks_base)

        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
                   new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _mock_response()
            handler = LiteLLMAIHandler()
            # Simulate Azure mode + a foreign key/base set by another provider during init
            handler.azure = True
            handler.api_base = "https://my-azure.openai.azure.com"
            monkeypatch.setattr(litellm, "api_key", foreign_key)
            await handler.chat_completion(
                model="databricks/databricks-claude-sonnet-4", system="sys", user="usr"
            )

        forwarded = mock_call.call_args[1]
        assert forwarded["model"] == "databricks/databricks-claude-sonnet-4", (
            f"databricks/* model must not be rewritten with an 'azure/' prefix. "
            f"kwargs had: {forwarded}"
        )
        assert "api_key" not in forwarded, (
            f"Foreign provider key must not be forwarded for databricks/* models even in Azure "
            f"mode. kwargs had: {forwarded}"
        )
        assert forwarded["api_base"] == databricks_base, (
            f"Databricks endpoint must come from DATABRICKS_API_BASE even in Azure mode. "
            f"kwargs had: {forwarded}"
        )

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


class _StopCall(Exception):
    """Sentinel raised from a patched LiteLLM transport once the api_key is captured."""


class TestPlaceholderDoesNotShadowProviderEnvVars:
    """Regression tests for issue #2544.

    The placeholder must not sit in the global ``litellm.api_key``: LiteLLM resolves
    several providers as ``api_key or litellm.api_key or <provider attr> or
    get_secret("<PROVIDER>_API_KEY")``, so a truthy placeholder there wins over the
    user's provider env var (OpenRouter, Azure, Mistral, ... ) and the request goes
    out with "dummy_key". Keeping the placeholder in ``litellm.openai_key`` — which
    LiteLLM only consults on the OpenAI/OpenAI-compatible paths — preserves the
    keyless local-endpoint use case without shadowing anything else.
    """

    @pytest.fixture(autouse=True)
    def restore_openai_key(self, monkeypatch):
        monkeypatch.setattr(litellm, "openai_key", None)
        monkeypatch.setattr(litellm, "api_key", None)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    @pytest.mark.asyncio
    async def test_keyless_init_clears_a_stale_provider_key(self, monkeypatch):
        """A keyless request must not inherit the previous request's provider key.

        __init__ mutates process-global LiteLLM state, so a provider branch from an
        earlier request (Groq/xAI/SambaNova/OpenRouter all write litellm.api_key) can
        still be sitting there. chat_completion() forwards any truthy non-placeholder
        litellm.api_key, so without an explicit reset a keyless request would
        authenticate with — and leak — the earlier request's credential.
        """
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
            "groq": type("Groq", (), {"key": "gsk-tenant-a"})(),
            "get": lambda self, key, default=None: (
                "gsk-tenant-a" if key == "GROQ.KEY" else default
            ),
        })()

        monkeypatch.setattr(litellm_handler, "get_settings", lambda: groq_settings)
        LiteLLMAIHandler()  # request 1: tenant A, Groq key lands in litellm.api_key
        assert litellm.api_key == "gsk-tenant-a"

        monkeypatch.setattr(litellm_handler, "get_settings", _make_settings)
        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
                   new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _mock_response()
            handler = LiteLLMAIHandler()  # request 2: tenant B, no keys configured
            await handler.chat_completion(
                model="openai/local-model", system="sys", user="usr"
            )

        assert "api_key" not in mock_call.call_args[1], (
            f"A keyless init must clear the previous request's provider key; "
            f"kwargs had: {mock_call.call_args[1].get('api_key')!r}"
        )

    def test_placeholder_not_stored_in_global_litellm_api_key(self):
        """With no OpenAI key configured, litellm.api_key must stay falsy."""
        LiteLLMAIHandler()

        assert litellm.api_key != DUMMY_LITELLM_API_KEY, (
            "The placeholder must not occupy litellm.api_key — it shadows provider "
            "env vars such as OPENROUTER_API_KEY in LiteLLM's resolution chain."
        )
        assert litellm.openai_key == DUMMY_LITELLM_API_KEY, (
            "The placeholder must still be available on the OpenAI-compatible path, "
            "so keyless local endpoints (vLLM, LM Studio, ...) keep working."
        )

    def test_placeholder_does_not_stick_across_handler_inits(self, monkeypatch):
        """A keyless request must not poison later requests that do configure OPENAI.KEY.

        LiteLLMAIHandler is constructed per request but writes to LiteLLM's process-wide
        globals. With the placeholder on litellm.api_key a single keyless init left
        "dummy_key" there for the lifetime of the process, and every later request
        resolved the placeholder even with OPENAI.KEY set — because __init__ only ever
        writes the real OpenAI key to litellm.openai_key, which sits *behind*
        litellm.api_key in the chain. Keeping the placeholder on openai_key means the
        real key simply replaces it.
        """
        from litellm import main as litellm_main

        openai_settings = type("Settings", (), {
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
            "openai": type("OpenAI", (), {"key": "sk-real-openai"})(),
            "get": lambda self, key, default=None: (
                "sk-real-openai" if key == "OPENAI.KEY" else default
            ),
        })()

        LiteLLMAIHandler()  # request 1: no OpenAI key configured
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: openai_settings)
        LiteLLMAIHandler()  # request 2: OPENAI.KEY configured

        captured = {}

        class _Capturing:
            def completion(self, *args, **kwargs):
                captured["api_key"] = kwargs.get("api_key")
                raise _StopCall

        monkeypatch.setattr(litellm_main, "openai_chat_completions", _Capturing())
        monkeypatch.setattr(litellm_main, "base_llm_http_handler", _Capturing())
        with contextlib.suppress(Exception):
            litellm.completion(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])

        assert captured.get("api_key") == "sk-real-openai", (
            f"A keyless init must not leave a placeholder that outlives it; "
            f"LiteLLM resolved: {captured.get('api_key')!r}"
        )

    def test_openrouter_env_key_wins_over_placeholder(self, monkeypatch):
        """End-to-end through LiteLLM's own resolution chain.

        Patches LiteLLM's internal HTTP handler to capture the api_key that would be
        sent for an ``openrouter/*`` model when the key comes from the native
        OPENROUTER_API_KEY env var rather than PR-Agent's [openrouter] settings.
        """
        from litellm import main as litellm_main

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-real-key")
        LiteLLMAIHandler()

        captured = {}

        class _CapturingHandler:
            def completion(self, **kwargs):
                captured["api_key"] = kwargs.get("api_key")
                raise _StopCall

        monkeypatch.setattr(litellm_main, "base_llm_http_handler", _CapturingHandler())
        # LiteLLM maps whatever the transport raises onto its own exception types,
        # so the sentinel comes back wrapped — the captured key is what matters.
        with contextlib.suppress(Exception):
            litellm.completion(
                model="openrouter/deepseek/deepseek-chat",
                messages=[{"role": "user", "content": "hi"}],
            )

        assert captured.get("api_key") == "sk-or-real-key", (
            f"OPENROUTER_API_KEY must not be shadowed by the placeholder; "
            f"LiteLLM resolved: {captured.get('api_key')!r}"
        )

    def test_openai_compatible_endpoint_still_gets_placeholder(self, monkeypatch):
        """Keyless local endpoints must still receive a key.

        The OpenAI SDK raises "The api_key client option must be set" when no key is
        resolved, which is exactly why the placeholder exists. Dropping it entirely
        (instead of moving it to litellm.openai_key) would break these setups.
        """
        from litellm import main as litellm_main

        LiteLLMAIHandler()

        captured = {}

        class _CapturingOpenAI:
            def completion(self, *args, **kwargs):
                captured["api_key"] = kwargs.get("api_key")
                raise _StopCall

        # LiteLLM routes the OpenAI path through either handler depending on the
        # EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER flag; capture on both.
        monkeypatch.setattr(litellm_main, "openai_chat_completions", _CapturingOpenAI())
        monkeypatch.setattr(litellm_main, "base_llm_http_handler", _CapturingOpenAI())
        with contextlib.suppress(Exception):
            litellm.completion(
                model="openai/local-model",
                api_base="http://localhost:8000/v1",
                messages=[{"role": "user", "content": "hi"}],
            )

        assert captured.get("api_key") == DUMMY_LITELLM_API_KEY, (
            f"Keyless OpenAI-compatible endpoints must still receive the placeholder; "
            f"LiteLLM resolved: {captured.get('api_key')!r}"
        )
