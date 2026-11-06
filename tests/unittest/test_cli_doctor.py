"""Preflight checks for `pr-agent doctor` (#4320)."""


import io
import json
import sys

import pytest

import pr_agent.cli as cli
import pr_agent.doctor as doctor
from pr_agent.algo.ai_handlers.cloud_auth import DUMMY_LITELLM_API_KEY
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


class _FakeHandler:
    """Stand in for LiteLLMAIHandler with a controllable captured key and ping."""

    def __init__(self, key="synthetic-key", ping_error=None):
        self._key = key
        self._ping_error = ping_error
        self._custom_llm_provider = ""
        self._anthropic_auth_token = None
        self._provider_request_params = {}
        self._provider_environment_api_keys = {}
        self._request_headers = {}
        self._openai_api_base_is_azure = False
        self._volcengine_ark_api_key = None
        self.deployment_id = None
        self.ping_calls = []

    def _route_model(self, model, deployment_id):
        return model

    def _captured_api_key(self, provider):
        return self._key

    async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
        self.ping_calls.append(model)
        if self._ping_error:
            raise self._ping_error
        return {"choices": [{"message": {"content": "ok"}}]}


@pytest.fixture
def cli_mode_settings(monkeypatch):
    snapshot = snapshot_settings((
        "config.model", "openai.api_base", "openai.api_type", "config.cli_mode",
        "azure_ad.client_id", "push_outputs", "openai_like.api_base",
        "custom_openai.api_base", "groq.api_base", "openrouter.api_base",
        "openrouter.key", "openai.key", "litellm.custom_llm_provider",
        "anthropic.key", "anthropic.api_base", "ollama.api_base", "ollama.api_key",
        "lemonade.api_base", "lemonade.api_key",
        "cloudflare.api_base", "publicai.api_base", "aihubmix.api_base",
        "volcengine.api_base", "litellm.extra_headers", "groq.key",
        "databricks.api_base", "databricks.api_key",
    ))
    for key in (
        "openai.api_base", "openai.api_type", "azure_ad.client_id",
        "openai_like.api_base", "custom_openai.api_base", "groq.api_base",
        "openrouter.api_base", "openrouter.key", "openai.key", "litellm.custom_llm_provider",
        "anthropic.key", "anthropic.api_base", "ollama.api_base", "ollama.api_key",
        "lemonade.api_base", "lemonade.api_key",
        "cloudflare.api_base", "publicai.api_base", "aihubmix.api_base",
        "volcengine.api_base", "litellm.extra_headers", "groq.key",
        "databricks.api_base", "databricks.api_key",
    ):
        get_settings().set(key, None)
    get_settings().set("config.model", "openai/gpt-4o")
    get_settings().set("push_outputs", {"enable": False})
    for variable in (
        "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_LIKE_API_BASE", "OPENAI_LIKE_API_KEY",
        "OPENROUTER_API_BASE", "OPENROUTER_API_KEY", "OR_API_KEY",
        "OPENAI_API_KEY", "OPENAI_KEY", "CLOUDFLARE_API_BASE", "CLOUDFLARE_API_KEY", "CLOUDFLARE_ACCOUNT_ID",
        "GROQ_API_BASE", "GROQ_API_KEY", "DATABRICKS_API_BASE", "DATABRICKS_API_KEY",
        "AZURE_API_BASE", "AZURE_OPENAI_ENDPOINT", "AZURE_API_KEY", "AZURE_OPENAI_API_KEY",
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_BASE", "ANTHROPIC_BASE_URL",
        "OLLAMA_API_KEY", "OLLAMA_API_BASE", "LLAMAFILE_API_KEY", "LLAMAFILE_API_BASE",
        "DOCKER_MODEL_RUNNER_API_KEY", "DOCKER_MODEL_RUNNER_API_BASE",
        "LEMONADE_API_KEY", "LEMONADE_API_BASE",
        "PUBLICAI_API_BASE", "PUBLICAI_API_KEY", "AIHUBMIX_API_BASE", "AIHUBMIX_API_KEY",
        "WATSONX_APIKEY", "WATSONX_API_KEY", "WX_API_KEY", "WATSONX_ZENAPIKEY",
        "WATSONX_TOKEN", "WATSONX_API_BASE", "WATSONX_URL", "WX_URL", "WML_URL",
        "VOLCENGINE_API_KEY", "VOLCENGINE_API_BASE", "ARK_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    # Keep provider-neutral CLI tests independent from ambient AWS credential discovery.
    monkeypatch.setattr(LiteLLMAIHandler, "_initialize_aws_request_credentials", lambda self, settings: None)
    try:
        yield get_settings()
    finally:
        restore_settings(snapshot)


def _install_handler(monkeypatch, **kwargs):
    fake = _FakeHandler(**kwargs)
    monkeypatch.setattr(doctor, "LiteLLMAIHandler", lambda: fake)
    return fake


def test_doctor_passes_and_exits_zero(monkeypatch, cli_mode_settings, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    _install_handler(monkeypatch, key="synthetic-key")

    assert cli.run(inargs=["doctor"]) == 0
    out = capsys.readouterr().out
    assert out == ("✓ config.model = openai/gpt-4o\n"
                   "✓ API key for 'openai' found (settings)\n"
                   "✓ model completion succeeded\n"
                   "3 of 3 checks passed\n")


@pytest.mark.parametrize("channels, expected_exit, summary", [
    (["stdout"], 0, "4 of 4 checks passed"),
    (["webhook"], 1, "3 of 4 checks passed"),
])
def test_doctor_checks_enabled_outputs_after_ping(monkeypatch, cli_mode_settings, capsys,
                                                channels, expected_exit, summary):
    cli_mode_settings.set("push_outputs", {"enable": True, "channels": channels})
    fake = _install_handler(monkeypatch)
    check = doctor._check_output_sinks

    def after_ping(cfg):
        assert fake.ping_calls == ["openai/gpt-4o"]
        return check(cfg)

    monkeypatch.setattr(doctor, "_check_output_sinks", after_ping)
    assert cli.run(inargs=["doctor"]) == expected_exit
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 5
    assert lines[2] == "✓ model completion succeeded"
    assert "push_outputs" in lines[3]
    assert lines[-1] == summary


def test_doctor_contains_settings_operation_errors_after_ping(monkeypatch, cli_mode_settings, capsys):
    fake = _install_handler(monkeypatch)

    class BrokenSettings:
        config = cli_mode_settings.config

        def get(self, key, default=None):
            assert fake.ping_calls == ["openai/gpt-4o"]
            raise ValueError("synthetic-token https://secret.example/hook /private/path")

    monkeypatch.setattr(doctor, "get_settings", lambda: BrokenSettings())
    assert cli.run(inargs=["doctor"]) == 1
    out = capsys.readouterr().out
    assert out.splitlines()[-2:] == ["✗ push_outputs configuration could not be checked", "3 of 4 checks passed"]
    assert all(value not in out for value in ("synthetic-token", "secret.example", "/private/path", "ValueError"))


def test_missing_key_names_the_env_variable_and_fails(monkeypatch, cli_mode_settings, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    _install_handler(monkeypatch, key=None)

    assert cli.run(inargs=["doctor"]) == 1
    out = capsys.readouterr().out
    assert "✗ no API key for 'openai'; set OPENAI_API_KEY" in out


def test_doctor_report_survives_stdout_that_cannot_encode(monkeypatch, cli_mode_settings):
    # A redirected Windows stdout uses cp1252, which has no check mark characters.
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    _install_handler(monkeypatch, key=None)

    assert cli.run(inargs=["doctor"]) == 1
    sys.stdout.flush()
    out = raw.getvalue().decode("utf-8")
    assert "✓ config.model = openai/gpt-4o\n" in out
    assert "✗ no API key for 'openai'; set OPENAI_API_KEY" in out
    assert out.endswith("2 of 3 checks passed\n")


def test_missing_provider_key_env_vars_shared_with_the_runner(monkeypatch, cli_mode_settings):
    _install_handler(monkeypatch, key="synthetic-key")
    assert doctor.missing_provider_key_env_vars() is None

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _install_handler(monkeypatch, key=None)
    result = doctor.missing_provider_key_env_vars()
    assert result is not None
    assert "OPENAI_API_KEY" in result[1]


def test_azure_ad_ambient_auth_defers_to_the_run(monkeypatch, cli_mode_settings):
    _install_handler(monkeypatch, key=None)
    get_settings().set("config.model", "azure/gpt-4o")
    get_settings().set("azure_ad.client_id", "synthetic-client-id")
    assert doctor.missing_provider_key_env_vars() is None


def test_azure_provider_defers_to_the_run_without_azure_ad_settings(monkeypatch, cli_mode_settings):
    _install_handler(monkeypatch, key=None)
    get_settings().set("config.model", "azure/gpt-4o")
    assert doctor.missing_provider_key_env_vars() is None


def test_bedrock_ambient_auth_defers_to_the_run(monkeypatch, cli_mode_settings):
    _install_handler(monkeypatch, key=None)
    get_settings().set("config.model", "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0")
    assert doctor.missing_provider_key_env_vars() is None


@pytest.mark.parametrize("model", ("bedrock/us.anthropic.claude-sonnet-4-5", "azure/gpt-4o"))
def test_doctor_passes_with_ambient_auth_and_no_static_key(monkeypatch, cli_mode_settings, capsys, model):
    # IRSA/IMDS or Azure AD: no static key, but the ping succeeds, so doctor must exit 0 (#4531).
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.delenv("AZURE_API_KEY", raising=False)
    _install_handler(monkeypatch, key=None)
    get_settings().set("config.model", model)

    assert cli.run(inargs=["doctor"]) == 0
    out = capsys.readouterr().out
    assert "(ambient auth or custom endpoint); ping decides" in out
    assert "3 of 3 checks passed" in out


def test_custom_endpoint_defers_to_the_run(monkeypatch, cli_mode_settings):
    _install_handler(monkeypatch, key=None)
    get_settings().set("openai.api_base", "https://gateway.example/v1")
    assert doctor.missing_provider_key_env_vars() is None


@pytest.mark.parametrize(
    ("model", "setting", "key_var", "uses_endpoint"),
    [
        ("anthropic/claude-3-5-sonnet-20241022", "anthropic.api_base", None, False),
        ("anthropic_text/claude-3-5-sonnet-20241022", "anthropic.api_base", "ANTHROPIC_API_KEY", False),
        ("groq/llama-3.3-70b-versatile", "groq.api_base", "GROQ_API_KEY", False),
        ("publicai/example-model", "publicai.api_base", "PUBLICAI_API_KEY", False),
        ("openai_like/gpt-4o", "openai_like.api_base", "OPENAI_LIKE_API_KEY", False),
        ("custom_openai/gpt-4o", "custom_openai.api_base", "OPENAI_API_KEY", False),
        ("cloudflare/@cf/meta/llama", "cloudflare.api_base", "CLOUDFLARE_API_KEY", False),
        ("databricks/example-model", "databricks.api_base", "DATABRICKS_API_KEY", True),
        ("openrouter/openai/gpt-4o", "openrouter.api_base", "OPENROUTER_API_KEY", True),
    ],
)
def test_endpoint_settings_must_be_used_by_the_real_handler(
    monkeypatch, cli_mode_settings, model, setting, key_var, uses_endpoint,
):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    endpoint = "https://gateway.example/v1"
    cli_mode_settings.set("config.model", model)
    cli_mode_settings.set(setting, endpoint)
    if key_var:
        monkeypatch.setenv(key_var, DUMMY_LITELLM_API_KEY)

    handler = LiteLLMAIHandler()
    provider = doctor._active_provider(model, handler)
    assert handler._captured_api_key(provider) == (DUMMY_LITELLM_API_KEY if key_var else None)
    captured = handler._provider_request_params.get(provider, {}).get("api_base")
    assert captured == (endpoint if uses_endpoint else None)
    params = handler._get_provider_request_params(model)
    assert (params.get("api_base") == endpoint) is uses_endpoint
    assert doctor._check_provider_key(handler, provider)[0] is uses_endpoint
    assert (doctor.missing_provider_key_env_vars() is None) is uses_endpoint

    async def fake_completion(self, model, system, user, temperature=0.2, **kwargs):
        return "ok", "stop"

    monkeypatch.setattr(LiteLLMAIHandler, "chat_completion", fake_completion)
    assert cli.run(inargs=["doctor"]) == (0 if uses_endpoint else 1)


@pytest.mark.parametrize(
    "model, overrides, key",
    [
        pytest.param("openai/gpt-4o", {"azure_ad.client_id": "synthetic-client-id"}, None, id="azure-ad"),
        pytest.param("openai/gpt-4o", {"openai.api_base": "https://gateway.example/v1"}, None, id="custom-endpoint"),
        pytest.param("custom_openai/gpt-4o", {"openai.api_base": "https://gateway.example/v1"},
                     None, id="openai-compatible-endpoint"),
        pytest.param("openai/gpt-4o", {"openai.api_base": "https://gateway.example/v1"},
                     DUMMY_LITELLM_API_KEY, id="custom-endpoint-placeholder"),
    ],
)
def test_keyless_auth_defers_to_the_ping(monkeypatch, cli_mode_settings, capsys, model, overrides, key):
    handler = _install_handler(monkeypatch, key=key)
    cli_mode_settings.set("config.model", model)
    for name, value in overrides.items():
        cli_mode_settings.set(name, value)

    assert doctor.missing_provider_key_env_vars() is None
    assert cli.run(inargs=["doctor"]) == 0
    assert handler.ping_calls == [model]
    out = capsys.readouterr().out
    assert "ping decides" in out
    assert "3 of 3 checks passed" in out


@pytest.mark.parametrize(
    "model, variable",
    [
        ("openai/gpt-4o", "OPENAI_BASE_URL"),
        ("custom_openai/gpt-4o", "OPENAI_API_BASE"),
        ("anthropic/claude-3-5-sonnet-20241022", "ANTHROPIC_BASE_URL"),
    ],
)
def test_keyless_endpoint_environment_defers_to_the_ping(monkeypatch, cli_mode_settings, model, variable):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    endpoint = "https://gateway.example/v1"
    cli_mode_settings.set("config.model", model)
    monkeypatch.setenv(variable, endpoint)

    handler = LiteLLMAIHandler()
    assert handler._get_provider_request_params(model)["api_base"] == endpoint
    ping_calls = []

    async def fake_completion(self, model, system, user, temperature=0.2, **kwargs):
        ping_calls.append(model)
        return "ok", "stop"

    monkeypatch.setattr(LiteLLMAIHandler, "chat_completion", fake_completion)
    assert doctor.missing_provider_key_env_vars() is None
    assert cli.run(inargs=["doctor"]) == 0
    assert ping_calls == [model]


@pytest.mark.parametrize("provider", ["watsonx", "watsonx_text"])
@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Authorization": "Bearer synthetic-token"}, True),
        ({"authorization": "ZenApiKey synthetic-zen"}, True),
        ({"Authorization": "Bearer dummy_key"}, False),
        ({"Authorization": "Bearer"}, False),
        ({"Authorization": ""}, False),
        ({"Authorization": "Bearer synthetic-token", "authorization": "Bearer dummy_key"}, False),
        ({"authorization": "Bearer dummy_key", "Authorization": "Bearer synthetic-token"}, True),
    ],
)
def test_watsonx_authorization_header_precedes_other_credentials(
    monkeypatch, cli_mode_settings, provider, headers, expected,
):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    model = f"{provider}/ibm/granite-3-8b-instruct"
    cli_mode_settings.set("config.model", model)
    cli_mode_settings.set("litellm.extra_headers", json.dumps(headers))
    monkeypatch.setenv("WATSONX_URL", "https://watsonx.example")
    monkeypatch.setenv("WATSONX_TOKEN", "synthetic-bearer-token")

    handler = LiteLLMAIHandler()
    # Host-configured Watsonx headers belong to _request_headers, not provider params.
    assert "headers" not in handler._provider_request_params.get(provider, {})
    final_value = next(value for name, value in reversed(tuple(headers.items()))
                       if name.lower() == "authorization")
    params = handler._get_provider_request_params(model, provider=provider, transport_provider=provider)
    assert params["headers"]["Authorization"] == final_value
    assert (doctor.missing_provider_key_env_vars() is None) is expected

    async def fake_completion(self, model, system, user, temperature=0.2, **kwargs):
        return "ok", "stop"

    monkeypatch.setattr(LiteLLMAIHandler, "chat_completion", fake_completion)
    assert cli.run(inargs=["doctor"]) == (0 if expected else 1)


@pytest.mark.parametrize("provider", ["watsonx", "watsonx_text"])
@pytest.mark.parametrize(
    ("auth_mode", "expected"),
    [
        ("url_only", False),
        ("bearer", True),
        ("zen", True),
        ("native_key", True),
        ("dummy_native_with_bearer", True),
        ("dummy_native_with_zen", True),
        ("dummy_bearer", False),
        ("dummy_zen", False),
        ("native_key_dummy_zen", False),
        ("dummy_bearer_valid_zen", False),
        ("valid_bearer_dummy_zen", True),
        ("native_key_dummy_bearer", False),
    ],
)
def test_watsonx_endpoint_does_not_replace_authentication(
    monkeypatch, cli_mode_settings, provider, auth_mode, expected,
):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    model = f"{provider}/ibm/granite-3-8b-instruct"
    cli_mode_settings.set("config.model", model)
    monkeypatch.setenv("WATSONX_URL", "https://watsonx.example")
    if auth_mode in ("bearer", "dummy_native_with_bearer", "dummy_bearer",
                     "dummy_bearer_valid_zen", "valid_bearer_dummy_zen", "native_key_dummy_bearer"):
        is_dummy = auth_mode in ("dummy_bearer", "dummy_bearer_valid_zen", "native_key_dummy_bearer")
        token = DUMMY_LITELLM_API_KEY if is_dummy else "synthetic-token"
        monkeypatch.setenv("WATSONX_TOKEN", token)
    if auth_mode in ("zen", "dummy_zen", "dummy_native_with_zen",
                     "native_key_dummy_zen", "dummy_bearer_valid_zen", "valid_bearer_dummy_zen"):
        is_dummy = auth_mode in ("dummy_zen", "native_key_dummy_zen", "valid_bearer_dummy_zen")
        zen = DUMMY_LITELLM_API_KEY if is_dummy else "synthetic-zen"
        monkeypatch.setenv("WATSONX_ZENAPIKEY", zen)
    if auth_mode in ("native_key", "dummy_native_with_bearer", "dummy_native_with_zen",
                     "native_key_dummy_bearer", "native_key_dummy_zen"):
        key = DUMMY_LITELLM_API_KEY if auth_mode.startswith("dummy_native") else "synthetic-key"
        monkeypatch.setenv("WATSONX_API_KEY", key)

    handler = LiteLLMAIHandler()
    assert handler._provider_request_params[provider]["api_base"] == "https://watsonx.example"
    assert doctor._active_provider(model, handler) == provider
    ok, detail = doctor._check_provider_key(handler, provider)
    assert ok is expected
    if auth_mode in ("dummy_zen", "native_key_dummy_zen"):
        assert "WATSONX_ZENAPIKEY" in detail
    elif auth_mode in ("dummy_bearer", "dummy_bearer_valid_zen", "native_key_dummy_bearer"):
        assert "WATSONX_TOKEN" in detail
    assert (doctor.missing_provider_key_env_vars() is None) is expected

    async def fake_completion(self, model, system, user, temperature=0.2, **kwargs):
        return "ok", "stop"

    monkeypatch.setattr(LiteLLMAIHandler, "chat_completion", fake_completion)
    assert cli.run(inargs=["doctor"]) == (0 if expected else 1)


@pytest.mark.parametrize("provider", ["watsonx", "watsonx_text"])
@pytest.mark.parametrize(
    ("source", "value", "valid_zen"),
    [
        ("WATSONX_TOKEN", "  ", False),
        ("WATSONX_TOKEN", " dummy_key ", False),
        ("WATSONX_TOKEN", " ", True),
        ("WATSONX_ZENAPIKEY", "\t ", False),
        ("WATSONX_ZENAPIKEY", " dummy_key ", False),
    ],
)
def test_watsonx_blank_or_padded_placeholder_overrides_native_key(
    monkeypatch, cli_mode_settings, provider, source, value, valid_zen,
):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    model = f"{provider}/ibm/granite-3-8b-instruct"
    cli_mode_settings.set("config.model", model)
    monkeypatch.setenv("WATSONX_URL", "https://watsonx.example")
    monkeypatch.setenv("WATSONX_API_KEY", "synthetic-native-key")
    monkeypatch.setenv(source, value)
    if valid_zen:
        monkeypatch.setenv("WATSONX_ZENAPIKEY", "synthetic-zen")

    handler = LiteLLMAIHandler()
    assert handler._captured_api_key(provider) == "synthetic-native-key"
    parameter = "token" if source == "WATSONX_TOKEN" else "zen_api_key"
    assert handler._provider_request_params[provider][parameter] == value
    ok, detail = doctor._check_provider_key(handler, provider)
    assert ok is False
    assert source in detail
    assert doctor.missing_provider_key_env_vars() == (provider, (source,))

    async def fake_completion(self, model, system, user, temperature=0.2, **kwargs):
        return "ok", "stop"

    monkeypatch.setattr(LiteLLMAIHandler, "chat_completion", fake_completion)
    assert cli.run(inargs=["doctor"]) == 1


def test_unavailable_registry_cannot_exempt_a_captured_endpoint(monkeypatch, cli_mode_settings):
    cli_mode_settings.set("config.model", "publicai/example-model")
    handler = _install_handler(monkeypatch, key=DUMMY_LITELLM_API_KEY)
    handler._provider_request_params = {"publicai": {"api_base": "https://gateway.example/v1"}}
    monkeypatch.setattr(doctor, "_resolve_provider_registry", lambda: None)

    assert doctor._check_provider_key(handler, "publicai")[0] is False
    assert doctor.missing_provider_key_env_vars() is not None
    assert cli.run(inargs=["doctor"]) == 1


@pytest.mark.parametrize(
    ("provider", "key_var", "base_var"),
    [
        ("publicai", "PUBLICAI_API_KEY", "PUBLICAI_API_BASE"),
        ("aihubmix", "AIHUBMIX_API_KEY", "AIHUBMIX_API_BASE"),
    ],
)
@pytest.mark.parametrize(
    ("has_endpoint", "model_fails", "expected_exit"),
    [(True, False, 0), (True, True, 1), (False, False, 1)],
)
def test_registry_only_endpoint_alias_uses_captured_handler_route(
    monkeypatch, cli_mode_settings, provider, key_var, base_var, has_endpoint, model_fails, expected_exit,
):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    model = f"{provider}/example-model"
    endpoint = "https://gateway.example/v1"
    cli_mode_settings.set("config.model", model)
    monkeypatch.setenv(key_var, DUMMY_LITELLM_API_KEY)
    if has_endpoint:
        monkeypatch.setenv(base_var, endpoint)

    handler = LiteLLMAIHandler()
    captured = handler._provider_request_params.get(provider, {}).get("api_base")
    assert captured == (endpoint if has_endpoint else None)
    assert handler._captured_api_key(provider) == DUMMY_LITELLM_API_KEY
    assert doctor._active_provider(model, handler) == provider
    if has_endpoint:
        routed = handler._get_provider_request_params(model, provider=provider, transport_provider=provider)
        assert routed["api_base"] == endpoint
    assert (doctor.missing_provider_key_env_vars() is None) is has_endpoint

    async def fake_completion(self, model, system, user, temperature=0.2, **kwargs):
        if model_fails:
            raise RuntimeError("gateway rejected request")
        return "ok", "stop"

    monkeypatch.setattr(LiteLLMAIHandler, "chat_completion", fake_completion)
    assert cli.run(inargs=["doctor"]) == expected_exit


@pytest.mark.parametrize(
    ("model", "key_var", "extra_var"),
    [
        ("cloudflare/@cf/meta/llama", "CLOUDFLARE_API_KEY", "CLOUDFLARE_ACCOUNT_ID"),
        ("openrouter/openai/gpt-4o", "OPENROUTER_API_KEY", None),
    ],
)
def test_generated_default_endpoint_does_not_bypass_placeholder_key(
    monkeypatch, cli_mode_settings, model, key_var, extra_var,
):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    cli_mode_settings.set("config.model", model)
    monkeypatch.setenv(key_var, DUMMY_LITELLM_API_KEY)
    if extra_var:
        monkeypatch.setenv(extra_var, "synthetic-account-id")

    handler = LiteLLMAIHandler()
    provider = doctor._active_provider(model, handler)
    assert handler._provider_request_params.get(provider, {}).get("api_base")
    assert doctor._check_provider_key(handler, provider)[0] is False
    assert doctor.missing_provider_key_env_vars() is not None


@pytest.mark.parametrize(
    "model, source",
    [
        ("custom_openai/gpt-4o", "settings"),
        ("openai_like/gpt-4o", "OPENAI_BASE_URL"),
        ("custom_openai/gpt-4o", "OPENAI_API_BASE"),
    ],
)
def test_azure_endpoint_does_not_exempt_openai_compatible_keys(monkeypatch, cli_mode_settings, model, source):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    cli_mode_settings.set("config.model", model)
    cli_mode_settings.set("openai.api_type", "azure")
    if source == "settings":
        cli_mode_settings.set("openai.api_base", "https://example.openai.azure.com/")
    else:
        monkeypatch.setenv(source, "https://example.openai.azure.com/")

    handler = LiteLLMAIHandler()
    assert doctor._active_provider(model, handler) == model.split("/", 1)[0]
    assert doctor.missing_provider_key_env_vars() is not None

    async def fake_completion(self, model, system, user, temperature=0.2, **kwargs):
        return "ok", "stop"

    monkeypatch.setattr(LiteLLMAIHandler, "chat_completion", fake_completion)
    assert cli.run(inargs=["doctor"]) == 1


@pytest.mark.parametrize(
    ("source", "expected_azure_base"),
    [
        ("settings", "https://azure.example"),
        ("AZURE_API_BASE", "https://azure.example"),
        ("AZURE_OPENAI_ENDPOINT", "https://azure.example"),
        ("OPENAI_BASE_URL", None),
        ("OPENAI_API_BASE", None),
    ],
)
def test_openai_model_in_azure_mode_uses_only_azure_endpoints(
    monkeypatch, cli_mode_settings, source, expected_azure_base,
):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    cli_mode_settings.set("openai.api_type", "azure")
    if source == "settings":
        cli_mode_settings.set("openai.api_base", "https://azure.example")
    else:
        monkeypatch.setenv(source, "https://azure.example")

    handler = LiteLLMAIHandler()
    assert doctor._active_provider("openai/gpt-4o", handler) == "azure"
    assert handler._provider_request_params.get("azure", {}).get("api_base") == expected_azure_base
    # Azure preflight intentionally defers static-key validation to the completion.
    assert doctor.missing_provider_key_env_vars() is None

    async def fake_completion(self, model, system, user, temperature=0.2, **kwargs):
        return "ok", "stop"

    monkeypatch.setattr(LiteLLMAIHandler, "chat_completion", fake_completion)
    assert cli.run(inargs=["doctor"]) == 0


@pytest.mark.parametrize(
    ("configured_provider", "source", "expected"),
    [
        ("openai", "OPENAI_BASE_URL", True),
        ("openai", "OPENAI_API_BASE", True),
        ("openai", "AZURE_API_BASE", False),
        ("openai", "AZURE_OPENAI_ENDPOINT", False),
        ("custom_openai", "OPENAI_BASE_URL", False),
    ],
)
def test_explicit_provider_override_ignores_unrelated_endpoint_aliases(
    monkeypatch, cli_mode_settings, configured_provider, source, expected,
):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    cli_mode_settings.set("openai.api_type", "azure")
    cli_mode_settings.set("litellm.custom_llm_provider", configured_provider)
    monkeypatch.setenv(source, "https://gateway.example/v1")
    handler = LiteLLMAIHandler()

    assert doctor._active_provider("openai/gpt-4o", handler) == configured_provider
    assert (doctor.missing_provider_key_env_vars() is None) is expected

    async def fake_completion(self, model, system, user, temperature=0.2, **kwargs):
        return "ok", "stop"

    monkeypatch.setattr(LiteLLMAIHandler, "chat_completion", fake_completion)
    assert cli.run(inargs=["doctor"]) == (0 if expected else 1)


@pytest.mark.parametrize("model", ["claude-3-5-sonnet-20241022", "gpt-4o", "openai/gpt-4o"])
def test_doctor_resolves_automatic_azure_routing(monkeypatch, cli_mode_settings, model):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    cli_mode_settings.set("config.model", model)
    cli_mode_settings.set("openai.api_type", "azure")
    cli_mode_settings.set("openai.api_base", "https://azure.example")
    handler = LiteLLMAIHandler()

    assert handler._route_model(model, None).startswith("azure/")
    assert doctor._active_provider(model, handler) == "azure"

    async def fake_completion(self, model, system, user, temperature=0.2, **kwargs):
        return "ok", "stop"

    monkeypatch.setattr(LiteLLMAIHandler, "chat_completion", fake_completion)
    assert doctor.missing_provider_key_env_vars() is None
    assert cli.run(inargs=["doctor"]) == 0


@pytest.mark.parametrize("token, expected", [
    (None, False), ("synthetic-bearer-token", True), (DUMMY_LITELLM_API_KEY, False),
    (" \t ", False), (" dummy_key ", False),
])
def test_doctor_accepts_captured_anthropic_bearer_token(monkeypatch, cli_mode_settings, token, expected):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    cli_mode_settings.set("config.model", "hosted-model")
    cli_mode_settings.set("litellm.custom_llm_provider", "anthropic")
    if token:
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", token)
    handler = LiteLLMAIHandler()

    assert handler._captured_api_key("anthropic") is None
    assert doctor._active_provider("hosted-model", handler) == "anthropic"
    assert doctor._check_provider_key(handler, "anthropic")[0] is expected
    assert handler._anthropic_auth_token == token

    async def fake_completion(self, model, system, user, temperature=0.2, **kwargs):
        return "ok", "stop"

    monkeypatch.setattr(LiteLLMAIHandler, "chat_completion", fake_completion)
    assert (doctor.missing_provider_key_env_vars() is None) is expected
    assert cli.run(inargs=["doctor"]) == (0 if expected else 1)


@pytest.mark.parametrize("provider", ["custom_openai", "openai_like"])
def test_compatible_override_inherits_openai_settings_key(monkeypatch, cli_mode_settings, provider):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_LIKE_API_KEY", raising=False)
    cli_mode_settings.set("openai.key", "synthetic-key")
    cli_mode_settings.set("litellm.custom_llm_provider", provider)
    handler = LiteLLMAIHandler()

    assert handler._captured_api_key(provider) is None
    assert handler._get_provider_request_params(
        f"{provider}/gpt-4o", provider=provider, transport_provider=provider,
    )["api_key"] == "synthetic-key"
    assert doctor._active_provider("gpt-4o", handler) == provider
    assert doctor._check_provider_key(handler, provider)[0]

    async def fake_completion(self, model, system, user, temperature=0.2, **kwargs):
        return "ok", "stop"

    monkeypatch.setattr(LiteLLMAIHandler, "chat_completion", fake_completion)
    assert cli.run(inargs=["doctor"]) == 0


@pytest.mark.parametrize(
    ("provider", "expected_request_key", "expected_ok"),
    [
        ("custom_openai", "synthetic-openai-env-key", True),
        ("openai_like", DUMMY_LITELLM_API_KEY, False),
    ],
)
def test_compatible_openai_environment_key_tracks_actual_handler_request(
    monkeypatch, cli_mode_settings, provider, expected_request_key, expected_ok,
):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    model = f"{provider}/gpt-4o"
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-env-key")
    cli_mode_settings.set("config.model", model)
    handler = LiteLLMAIHandler()

    assert handler._provider_environment_api_keys["openai"] == "synthetic-openai-env-key"
    assert handler._get_provider_request_params(
        model, provider=provider, transport_provider=provider,
    )["api_key"] == expected_request_key
    assert (doctor.missing_provider_key_env_vars() is None) is expected_ok


def test_openai_like_native_placeholder_takes_precedence(monkeypatch, cli_mode_settings):
    handler = _install_handler(monkeypatch, key=DUMMY_LITELLM_API_KEY)
    handler._provider_request_params = {"openai": {"api_key": "synthetic-key"}}
    handler._openai_api_base_is_azure = False

    assert doctor._check_provider_key(handler, "openai_like")[0] is False


@pytest.mark.parametrize("endpoint_source", ["settings", "environment"])
def test_registry_compatible_provider_uses_openai_endpoint(monkeypatch, cli_mode_settings, endpoint_source):
    handler = _install_handler(monkeypatch, key=None)
    monkeypatch.setattr(doctor, "_is_openai_compatible_request_provider", lambda provider: provider == "groq")
    cli_mode_settings.set("config.model", "groq/example-model")
    if endpoint_source == "settings":
        cli_mode_settings.set("openai.api_base", "https://gateway.example/v1")
    else:
        monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example/v1")

    assert doctor.missing_provider_key_env_vars() is None
    assert cli.run(inargs=["doctor"]) == 0
    assert handler.ping_calls == ["groq/example-model"]


@pytest.mark.parametrize("native_identity, azure", [(False, False), (False, True), (True, False)])
def test_registry_compatible_provider_requires_key_without_fallback(
    monkeypatch, cli_mode_settings, native_identity, azure,
):
    handler = _install_handler(monkeypatch, key=None)
    monkeypatch.setattr(doctor, "_is_openai_compatible_request_provider", lambda provider: provider == "groq")
    cli_mode_settings.set("config.model", "groq/example-model")
    if azure:
        cli_mode_settings.set("openai.api_type", "azure")
    if native_identity or azure:
        cli_mode_settings.set("openai.api_base", "https://gateway.example/v1")
    if native_identity:
        handler._provider_request_params = {"groq": {"organization": "native-provider"}}

    assert doctor.missing_provider_key_env_vars() == ("groq", ("GROQ_API_KEY",))
    assert cli.run(inargs=["doctor"]) == 1
    assert handler.ping_calls == ["groq/example-model"]


@pytest.mark.parametrize(
    ("model", "native_key", "ark_key", "expected_ok", "request_key", "uses_gateway"),
    [
        ("volcengine/responses/probe", None, DUMMY_LITELLM_API_KEY, False, DUMMY_LITELLM_API_KEY, False),
        ("volcengine/responses/probe", None, "real-ark-key", True, "real-ark-key", False),
        ("volcengine/responses/probe", "real-native-key", DUMMY_LITELLM_API_KEY, True, "real-native-key", False),
        ("volcengine/responses/probe", DUMMY_LITELLM_API_KEY, "real-ark-key", False, DUMMY_LITELLM_API_KEY, False),
        ("volcengine/responses/probe", None, None, True, DUMMY_LITELLM_API_KEY, True),
        ("volcengine/chat-model", None, DUMMY_LITELLM_API_KEY, True, DUMMY_LITELLM_API_KEY, True),
        ("volcengine/chat-model", None, "real-ark-key", True, DUMMY_LITELLM_API_KEY, True),
    ],
)
def test_volcengine_responses_uses_selected_key_before_gateway_fallback(
    monkeypatch, cli_mode_settings, model, native_key, ark_key, expected_ok, request_key, uses_gateway,
):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    cli_mode_settings.set("config.model", model)
    cli_mode_settings.set("openai.api_base", "https://gateway.example/v1")
    if native_key is not None:
        monkeypatch.setenv("VOLCENGINE_API_KEY", native_key)
    if ark_key is not None:
        monkeypatch.setenv("ARK_API_KEY", ark_key)

    handler = LiteLLMAIHandler()
    assert doctor._active_provider(model, handler) == "volcengine"
    assert handler._captured_api_key("volcengine") == native_key
    assert handler._volcengine_ark_api_key == ark_key

    request_params = handler._get_provider_request_params(
        model, provider="volcengine", transport_provider="volcengine", transport_model=model,
    )
    assert request_params["api_key"] == request_key
    assert (request_params.get("api_base") == "https://gateway.example/v1") is uses_gateway
    assert doctor._check_provider_key(handler, "volcengine")[0] is expected_ok
    assert (doctor.missing_provider_key_env_vars() is None) is expected_ok

    async def fake_completion(self, model, system, user, temperature=0.2, **kwargs):
        return "ok", "stop"

    monkeypatch.setattr(LiteLLMAIHandler, "chat_completion", fake_completion)
    assert cli.run(inargs=["doctor"]) == (0 if expected_ok else 1)


@pytest.mark.parametrize(
    ("model", "custom_provider"),
    [("volcengine/responses/probe", None), ("responses/probe", "volcengine")],
)
@pytest.mark.parametrize(
    ("native_key", "ark_key"),
    [(None, DUMMY_LITELLM_API_KEY), (DUMMY_LITELLM_API_KEY, None)],
)
@pytest.mark.parametrize("native_endpoint", [False, True])
def test_volcengine_native_endpoint_defers_placeholder_on_actual_route(
    monkeypatch, cli_mode_settings, model, custom_provider, native_key, ark_key, native_endpoint,
):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    cli_mode_settings.set("config.model", model)
    cli_mode_settings.set("openai.api_base", "https://openai-gateway.example/v1")
    if custom_provider:
        cli_mode_settings.set("litellm.custom_llm_provider", custom_provider)
    if native_endpoint:
        monkeypatch.setenv("VOLCENGINE_API_BASE", "https://volcengine-gateway.example/v1")
    if native_key is not None:
        monkeypatch.setenv("VOLCENGINE_API_KEY", native_key)
    if ark_key is not None:
        monkeypatch.setenv("ARK_API_KEY", ark_key)

    handler = LiteLLMAIHandler()
    assert doctor._active_provider(model, handler) == "volcengine"
    routed = handler._route_model_for_request(model, custom_provider or "", handler.deployment_id)
    transport = custom_provider or handler._resolve_request_transport_provider(routed) or "volcengine"
    params = handler._get_provider_request_params(
        routed, provider="volcengine", transport_provider=transport, transport_model=routed,
    )
    assert params["api_key"] == DUMMY_LITELLM_API_KEY
    assert params.get("api_base") == ("https://volcengine-gateway.example/v1" if native_endpoint else None)
    assert doctor._check_provider_key(handler, "volcengine")[0] is native_endpoint
    assert (doctor.missing_provider_key_env_vars() is None) is native_endpoint

    async def fake_completion(self, model, system, user, temperature=0.2, **kwargs):
        return "ok", "stop"

    monkeypatch.setattr(LiteLLMAIHandler, "chat_completion", fake_completion)
    assert cli.run(inargs=["doctor"]) == (0 if native_endpoint else 1)


def test_keyless_auth_still_fails_when_the_ping_fails(monkeypatch, cli_mode_settings, capsys):
    handler = _install_handler(monkeypatch, key=None, ping_error=RuntimeError("Authentication failed"))
    cli_mode_settings.set("config.model", "azure/gpt-4o")

    assert cli.run(inargs=["doctor"]) == 1
    assert handler.ping_calls == ["azure/gpt-4o"]
    out = capsys.readouterr().out
    assert "✗ model completion failed: Authentication failed" in out
    assert "2 of 3 checks passed" in out


def test_cloudflare_account_id_is_not_a_custom_endpoint(monkeypatch, cli_mode_settings, capsys):
    _install_handler(monkeypatch, key=None)
    cli_mode_settings.set("config.model", "cloudflare/@cf/meta/llama-3.1-8b-instruct")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "synthetic-account-id")

    assert doctor.missing_provider_key_env_vars() == ("cloudflare", ("CLOUDFLARE_API_KEY",))
    assert cli.run(inargs=["doctor"]) == 1
    assert "✗ no API key for 'cloudflare'" in capsys.readouterr().out


@pytest.mark.parametrize("model", ["openai/gpt-4o", "openrouter/openai/gpt-4o"])
def test_placeholder_key_fails(monkeypatch, cli_mode_settings, capsys, model):
    _install_handler(monkeypatch, key=DUMMY_LITELLM_API_KEY)
    cli_mode_settings.set("config.model", model)

    assert cli.run(inargs=["doctor"]) == 1
    assert "the placeholder; set a real key" in capsys.readouterr().out


def test_bad_ping_reports_the_reason(monkeypatch, cli_mode_settings, capsys):
    _install_handler(monkeypatch, key="synthetic-key",
                     ping_error=RuntimeError("Connection error to https://api.example"))

    assert cli.run(inargs=["doctor"]) == 1
    out = capsys.readouterr().out
    assert "✗ model completion failed: Connection error to https://api.example" in out
    assert "2 of 3 checks passed" in out


def test_ping_failure_redacts_credentials_quoted_by_the_provider(monkeypatch, cli_mode_settings, capsys):
    secret = "sk-proj-synthetic-key-value-for-tests-0123456789abcdef"
    _install_handler(monkeypatch, key="synthetic-key",
                     ping_error=RuntimeError(f"401 invalid key {secret} at https://api.example"))

    assert cli.run(inargs=["doctor"]) == 1
    out = capsys.readouterr().out
    assert secret not in out
    assert "<redacted>" in out
    assert "401 invalid key" in out


def test_doctor_needs_no_pr_url_and_skips_provider_setup(monkeypatch, cli_mode_settings):
    constructed = []

    def fail_agent(*_args, **_kwargs):
        constructed.append("PRAgent")
        raise AssertionError("doctor must not dispatch to PRAgent")

    monkeypatch.setattr(cli, "PRAgent", fail_agent)
    _install_handler(monkeypatch)

    assert cli.run(inargs=["doctor"]) == 0
    assert constructed == []


def test_key_never_appears_in_output(monkeypatch, cli_mode_settings, capsys):
    _install_handler(monkeypatch, key="synthetic-secret-value")

    cli.run(inargs=["doctor"])
    assert "synthetic-secret-value" not in capsys.readouterr().out


def test_custom_base_url_keeps_the_openai_provider(cli_mode_settings):
    # The handler captures openai.key and openai.api_base under "openai", so the key check must look there.
    get_settings().set("openai.api_base", "https://gateway.example/v1")
    assert doctor._active_provider("gpt-4o") == "openai"


def test_unprefixed_non_openai_model_resolves_via_litellm(cli_mode_settings, monkeypatch):
    monkeypatch.setattr("litellm.get_llm_provider", lambda model: (model, "anthropic"))
    assert doctor._active_provider("claude-3-5-sonnet-20241022") == "anthropic"


@pytest.mark.parametrize(
    ("model", "expected_provider", "requires_key"),
    [
        ("text-completion-openai/gpt-4o", "openai", True),
        ("anthropic_text/claude-3-5-sonnet-20241022", "anthropic", True),
        ("azure_text/gpt-4o", "azure", False),
    ],
)
def test_prefixed_provider_aliases_share_handler_key_policy(
    monkeypatch, cli_mode_settings, model, expected_provider, requires_key,
):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    assert LiteLLMAIHandler()._resolve_request_provider(model) == expected_provider
    handler = _install_handler(monkeypatch, key=None)
    cli_mode_settings.set("config.model", model)

    assert doctor._active_provider(model, handler) == expected_provider
    assert (doctor.missing_provider_key_env_vars() is not None) is requires_key
    assert cli.run(inargs=["doctor"]) == (1 if requires_key else 0)
    assert handler.ping_calls == [model]


@pytest.mark.parametrize(
    ("litellm_provider", "expected"),
    [("text-completion-openai", "openai"), ("anthropic_text", "anthropic")],
)
def test_unprefixed_litellm_provider_aliases_are_normalized(
    monkeypatch, cli_mode_settings, litellm_provider, expected,
):
    monkeypatch.setattr("litellm.get_llm_provider", lambda model: (model, litellm_provider))
    assert doctor._active_provider("bare-model") == expected


@pytest.mark.parametrize(
    ("gateway", "azure", "native_key", "expected_key", "expected_ok"),
    [
        ("settings", False, None, "gateway-key", True),
        ("environment", False, None, "environment-key", True),
        ("placeholder", False, None, DUMMY_LITELLM_API_KEY, True),
        ("environment-placeholder", False, None, DUMMY_LITELLM_API_KEY, True),
        ("placeholder-key-only", False, None, None, False),
        ("placeholder", True, None, None, False),
        ("settings", True, None, None, False),
        ("key-only", False, None, None, False),
        ("settings", False, "native-key", "native-key", True),
        ("settings", False, DUMMY_LITELLM_API_KEY, DUMMY_LITELLM_API_KEY, False),
    ],
)
def test_openrouter_inherits_only_eligible_openai_gateway_credentials(
    monkeypatch, cli_mode_settings, gateway, azure, native_key, expected_key, expected_ok,
):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    model = "openrouter/anthropic/claude-3.5-sonnet"
    cli_mode_settings.set("config.model", model)
    if gateway not in ("key-only", "placeholder-key-only"):
        cli_mode_settings.set("openai.api_base", "https://openrouter.ai/api/v1")
    if gateway in ("environment", "environment-placeholder"):
        environment_key = DUMMY_LITELLM_API_KEY if gateway == "environment-placeholder" else "environment-key"
        monkeypatch.setenv("OPENAI_API_KEY", environment_key)
    else:
        settings_key = DUMMY_LITELLM_API_KEY if gateway.startswith("placeholder") else "gateway-key"
        cli_mode_settings.set("openai.key", settings_key)
    if azure:
        cli_mode_settings.set("openai.api_type", "azure")
    if native_key:
        cli_mode_settings.set("openrouter.key", native_key)

    handler = LiteLLMAIHandler()
    assert doctor._active_provider(model, handler) == "openrouter"
    assert handler._captured_api_key("openrouter") == native_key

    request_params = handler._get_provider_request_params(
        model, provider="openrouter", transport_provider="openrouter",
    )
    assert request_params.get("api_key") == expected_key
    assert (doctor.missing_provider_key_env_vars() is None) is expected_ok

    async def fake_completion(self, model, system, user, temperature=0.2, **kwargs):
        return "ok", "stop"

    monkeypatch.setattr(LiteLLMAIHandler, "chat_completion", fake_completion)
    assert cli.run(inargs=["doctor"]) == (0 if expected_ok else 1)


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        ("ollama/llama3", "ollama"),
        ("ollama_chat/llama3", "ollama"),
        ("llamafile/llama3", "llamafile"),
        ("docker_model_runner/llama3", "docker_model_runner"),
        ("lemonade/Qwen3-4B-GGUF", "lemonade"),
    ],
)
@pytest.mark.parametrize("key", [None, DUMMY_LITELLM_API_KEY])
def test_keyless_local_default_endpoints_and_placeholders(
    monkeypatch, cli_mode_settings, model, provider, key,
):
    handler = _install_handler(monkeypatch, key=key)
    cli_mode_settings.set("config.model", model)

    assert doctor._active_provider(model, handler) == provider
    assert doctor.missing_provider_key_env_vars() is None
    assert cli.run(inargs=["doctor"]) == 0
    assert handler.ping_calls == [model]


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        ("ollama_chat/llama3", "ollama"),
        ("llamafile/llama3", "llamafile"),
        ("docker_model_runner/llama3", "docker_model_runner"),
        ("lemonade/Qwen3-4B-GGUF", "lemonade"),
    ],
)
def test_keyless_local_placeholder_still_fails_on_model_error(
    monkeypatch, cli_mode_settings, model, provider,
):
    handler = _install_handler(
        monkeypatch, key=DUMMY_LITELLM_API_KEY, ping_error=RuntimeError("Request rejected"),
    )
    cli_mode_settings.set("config.model", model)

    assert doctor._active_provider(model, handler) == provider
    assert doctor.missing_provider_key_env_vars() is None
    assert cli.run(inargs=["doctor"]) == 1
    assert handler.ping_calls == [model]


@pytest.mark.parametrize(
    ("captured_key", "has_token", "expected_ok"),
    [
        (None, False, False),
        (None, True, True),
        (DUMMY_LITELLM_API_KEY, False, False),
        (DUMMY_LITELLM_API_KEY, True, False),
        ("real-native-key", False, True),
        ("real-native-key", True, True),
    ],
)
def test_anthropic_token_requires_absent_native_key(
    monkeypatch, cli_mode_settings, captured_key, has_token, expected_ok,
):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    cli_mode_settings.set("config.model", "hosted-model")
    cli_mode_settings.set("litellm.custom_llm_provider", "anthropic")
    if captured_key:
        cli_mode_settings.set("anthropic.key", captured_key)
    if has_token:
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-bearer-token")
    handler = LiteLLMAIHandler()

    assert handler._captured_api_key("anthropic") == captured_key
    assert handler._anthropic_auth_token == ("synthetic-bearer-token" if has_token else None)
    if captured_key == DUMMY_LITELLM_API_KEY and has_token:
        from litellm.llms.anthropic.common_utils import AnthropicModelInfo

        request_params = handler._get_provider_request_params(
            "hosted-model", provider="anthropic", transport_provider="anthropic",
        )
        assert request_params["api_key"] == DUMMY_LITELLM_API_KEY
        # LiteLLM's native header builder prefers the explicit API key over the bearer token.
        headers = AnthropicModelInfo().get_anthropic_headers(
            api_key=request_params["api_key"], auth_token=handler._anthropic_auth_token,
        )
        assert headers["x-api-key"] == DUMMY_LITELLM_API_KEY
        assert "authorization" not in headers
    assert doctor._check_provider_key(handler, "anthropic")[0] is expected_ok
    assert (doctor.missing_provider_key_env_vars() is None) is expected_ok

    async def fake_completion(self, model, system, user, temperature=0.2, **kwargs):
        return "ok", "stop"

    monkeypatch.setattr(LiteLLMAIHandler, "chat_completion", fake_completion)
    assert cli.run(inargs=["doctor"]) == (0 if expected_ok else 1)


def test_unresolvable_model_id_defers_to_the_ping(cli_mode_settings, monkeypatch):
    def explode(model):
        raise ValueError("no provider")

    monkeypatch.setattr("litellm.get_llm_provider", explode)
    assert doctor._active_provider("definitely-not-a-model") == "unknown"


def test_watsonx_native_token_takes_precedence_over_zen_key():
    from litellm.llms.watsonx.common_utils import IBMWatsonXMixin

    headers = IBMWatsonXMixin.validate_environment(
        None, headers={}, model="ibm/granite-3-8b-instruct", messages=[],
        optional_params={"token": DUMMY_LITELLM_API_KEY, "zen_api_key": "synthetic-zen"},
        litellm_params={}, api_key=DUMMY_LITELLM_API_KEY, api_base="https://watsonx.example",
    )
    assert headers["Authorization"] == f"Bearer {DUMMY_LITELLM_API_KEY}"

def test_doctor_fails_loudly_for_a_missing_handler_snapshot(monkeypatch, cli_mode_settings):
    handler = _install_handler(monkeypatch, key=None)
    cli_mode_settings.set("config.model", "openrouter/openai/gpt-4o")
    del handler._provider_request_params

    with pytest.raises(AttributeError, match="_provider_request_params"):
        doctor.missing_provider_key_env_vars()
