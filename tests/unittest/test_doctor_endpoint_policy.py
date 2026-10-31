"""Regression checks for provider endpoint and credential preflight."""

import pytest

from pr_agent import cli, doctor
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


class _StubHandler:
    def __init__(self):
        self._custom_llm_provider = ""
        self._provider_request_params = {}
        self._provider_environment_api_keys = {}
        self.pings = []

    def _captured_api_key(self, provider):
        return None

    async def chat_completion(self, model, system, user, **kwargs):
        self.pings.append(model)
        return "ok", "stop"


@pytest.fixture
def isolated_settings(monkeypatch):
    keys = (
        "config.model", "config.cli_mode", "push_outputs", "openai.api_type",
        "openai.api_base", "openai.key", "litellm.custom_llm_provider",
        "azure_ad.client_id",
    )
    snapshot = snapshot_settings(keys)
    settings = get_settings()
    for key in keys:
        settings.set(key, None)
    settings.set("config.model", "openai/gpt-4o")
    settings.set("push_outputs", {"enable": False})
    for name in (
        "OPENAI_BASE_URL", "OPENAI_API_BASE", "AZURE_API_BASE",
        "AZURE_OPENAI_ENDPOINT", "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY", "OPENAI_LIKE_API_KEY", "CLOUDFLARE_ACCOUNT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    try:
        yield settings
    finally:
        restore_settings(snapshot)


@pytest.mark.parametrize(
    ("model", "explicit_provider", "azure", "endpoint_var", "valid"),
    [
        ("anthropic/claude", "", False, "ANTHROPIC_BASE_URL", True),
        ("openai/gpt-4o", "", False, "OPENAI_BASE_URL", True),
        ("openai/gpt-4o", "", True, "AZURE_API_BASE", True),
        ("openai/gpt-4o", "", True, "AZURE_OPENAI_ENDPOINT", True),
        ("openai/gpt-4o", "", True, "OPENAI_BASE_URL", False),
        ("openai/gpt-4o", "openai", True, "OPENAI_BASE_URL", True),
        ("openai/gpt-4o", "openai", True, "AZURE_API_BASE", False),
        ("openai/gpt-4o", "custom_openai", True, "OPENAI_BASE_URL", False),
        ("cloudflare/example", "", False, "CLOUDFLARE_ACCOUNT_ID", False),
    ],
)
def test_endpoint_aliases_follow_effective_provider(
    monkeypatch, isolated_settings, model, explicit_provider, azure, endpoint_var, valid,
):
    handler = _StubHandler()
    handler._custom_llm_provider = explicit_provider
    isolated_settings.set("config.model", model)
    if azure:
        isolated_settings.set("openai.api_type", "azure")
    monkeypatch.setenv(endpoint_var, "https://gateway.example/v1")
    monkeypatch.setattr(doctor, "LiteLLMAIHandler", lambda: handler)

    assert (doctor.missing_provider_key_env_vars() is None) is valid
    assert cli.run(inargs=["doctor"]) == (0 if valid else 1)
    assert handler.pings == [model]


@pytest.mark.parametrize("provider", ["custom_openai", "openai_like"])
def test_compatible_provider_inherits_openai_settings_key(
    isolated_settings, provider,
):
    isolated_settings.set("openai.key", "example-credential")
    isolated_settings.set("litellm.custom_llm_provider", provider)
    handler = LiteLLMAIHandler()

    assert doctor._active_provider("gpt-4o", handler) == provider
    assert handler._captured_api_key(provider) is None
    assert handler._get_provider_request_params(
        f"{provider}/gpt-4o", provider=provider, transport_provider=provider,
    )["api_key"] == "example-credential"
    assert doctor._check_provider_key(handler, provider)[0] is True
