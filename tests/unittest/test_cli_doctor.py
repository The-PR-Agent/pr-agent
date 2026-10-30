"""Preflight checks for `pr-agent doctor` (#4320)."""


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
        self.ping_calls = []

    def _captured_api_key(self, provider):
        return self._key

    async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
        self.ping_calls.append(model)
        if self._ping_error:
            raise self._ping_error
        return {"choices": [{"message": {"content": "ok"}}]}


@pytest.fixture
def cli_mode_settings():
    snapshot = snapshot_settings(("config.model", "openai.api_base", "config.cli_mode", "azure_ad.client_id",
                                  "push_outputs"))
    get_settings().set("config.model", "openai/gpt-4o")
    get_settings().set("push_outputs", {"enable": False})
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


def test_custom_endpoint_defers_to_the_run(monkeypatch, cli_mode_settings):
    _install_handler(monkeypatch, key=None)
    get_settings().set("openai.api_base", "https://gateway.example/v1")
    assert doctor.missing_provider_key_env_vars() is None


def test_placeholder_key_fails(monkeypatch, cli_mode_settings, capsys):
    _install_handler(monkeypatch, key=DUMMY_LITELLM_API_KEY)

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


def test_unresolvable_model_id_defers_to_the_ping(cli_mode_settings, monkeypatch):
    def explode(model):
        raise ValueError("no provider")

    monkeypatch.setattr("litellm.get_llm_provider", explode)
    assert doctor._active_provider("definitely-not-a-model") == "unknown"
