"""Bedrock guardrail pass-through ([bedrock] guardrail_* settings) for the Converse route."""

from unittest.mock import AsyncMock, MagicMock, patch

import openai
import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from pr_agent.config_security import REPO_OVERRIDABLE_KEYS_BY_HOST_SECTION

MODEL = "bedrock/converse/arn:aws:bedrock:eu-central-1:123456789012:application-inference-profile/abc123"


@pytest.fixture(autouse=True)
def isolate_aws_environment(monkeypatch):
    names = set(litellm_handler.AWS_CREDENTIAL_CHAIN_ENV_VARS) | {
        "AWS_USE_IMDS",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION_NAME",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_BEARER_TOKEN_BEDROCK",
    }
    for name in names:
        monkeypatch.delenv(name, raising=False)


class _Box:
    def __init__(self, values=None, **attrs):
        self._values = values or {}
        for key, value in attrs.items():
            setattr(self, key, value)

    def get(self, key, default=None):
        return self._values.get(key, default)


class _Settings:
    def __init__(self, values):
        self.config = _Box(
            reasoning_effort=None,
            ai_timeout=30,
            custom_reasoning_model=False,
            max_model_tokens=32000,
            verbosity_level=0,
            model=MODEL,
        )
        self.litellm = _Box()
        self._values = {
            "aws.AWS_ACCESS_KEY_ID": "test-access-key",
            "aws.AWS_SECRET_ACCESS_KEY": "test-secret-key",
            "aws.AWS_REGION_NAME": "eu-central-1",
            **values,
        }

    def get(self, key, default=None):
        return self._values.get(key, default)


def _mock_response():
    mock = MagicMock()
    response = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    mock.usage = None
    mock.__getitem__.side_effect = response.__getitem__
    mock.dict.return_value = response
    return mock


def _handler(monkeypatch, identifier="", version="", trace=None):
    values = {"BEDROCK.GUARDRAIL_IDENTIFIER": identifier, "BEDROCK.GUARDRAIL_VERSION": version}
    if trace is not None:
        values["BEDROCK.GUARDRAIL_TRACE"] = trace
    settings = _Settings(values)
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    return litellm_handler.LiteLLMAIHandler()


async def _call(handler, model=MODEL):
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        await handler.chat_completion(model=model, system="sys", user="usr")
    return mock_call


@pytest.mark.asyncio
async def test_guardrail_config_passed_for_bedrock_model(monkeypatch):
    mock_call = await _call(_handler(monkeypatch, "gr-123", 3))

    assert mock_call.call_args.kwargs["guardrailConfig"] == {
        "guardrailIdentifier": "gr-123",
        "guardrailVersion": "3",
        "trace": "disabled",
    }


@pytest.mark.asyncio
async def test_guardrail_trace_is_forwarded(monkeypatch):
    mock_call = await _call(_handler(monkeypatch, "gr-123", "DRAFT", "Enabled"))

    assert mock_call.call_args.kwargs["guardrailConfig"]["trace"] == "enabled"


@pytest.mark.asyncio
async def test_no_guardrail_when_unset(monkeypatch):
    mock_call = await _call(_handler(monkeypatch))

    assert "guardrailConfig" not in mock_call.call_args.kwargs


@pytest.mark.asyncio
async def test_guardrail_not_applied_to_non_bedrock_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    mock_call = await _call(_handler(monkeypatch, "gr-123", "1"), model="gpt-4o")

    assert "guardrailConfig" not in mock_call.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identifier", "version", "trace"),
    [("gr-123", "", None), ("", "1", None), ("gr-123", "1", "verbose")],
)
async def test_invalid_config_raises_value_error_without_calling_model(monkeypatch, identifier, version, trace):
    handler = _handler(monkeypatch, identifier, version, trace)
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        with pytest.raises(ValueError) as exc_info:
            await handler.chat_completion(model=MODEL, system="sys", user="usr")

    assert not isinstance(exc_info.value, openai.APIError)
    assert "BEDROCK.GUARDRAIL" in str(exc_info.value)
    assert mock_call.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "bedrock/invoke/anthropic.claude-v2",
        "bedrock/anthropic.claude-v2",
        "bedrock/arn:aws:bedrock:eu-central-1:123456789012:application-inference-profile/abc123",
        "bedrock_mantle/openai.gpt-oss-120b",
    ],
)
async def test_routes_without_guardrail_support_fail_closed(monkeypatch, model):
    with pytest.raises(ValueError, match="Converse route"):
        await _call(_handler(monkeypatch, "gr-123", "1"), model=model)


@pytest.mark.asyncio
async def test_health_probe_applies_guardrail(monkeypatch):
    completion = AsyncMock(return_value=_mock_response())
    await _handler(monkeypatch, "gr-123", "1").probe_completion(MODEL, _completion=completion)

    assert completion.call_args.kwargs["guardrailConfig"]["guardrailIdentifier"] == "gr-123"


@pytest.mark.asyncio
async def test_guardrail_intervention_raises_instead_of_returning_block_message(monkeypatch):
    handler = _handler(monkeypatch, "gr-123", "1")
    blocked = MagicMock()
    response = {"choices": [{"message": {"content": "Sorry, blocked."}, "finish_reason": "content_filter"}]}
    blocked.usage = None
    blocked.__getitem__.side_effect = response.__getitem__
    blocked.dict.return_value = response
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = blocked
        with pytest.raises(ValueError, match="guardrail gr-123 \\(version 1\\) blocked"):
            await handler.chat_completion(model=MODEL, system="sys", user="usr")

    assert mock_call.call_count == 1


def test_bedrock_section_is_host_only():
    assert REPO_OVERRIDABLE_KEYS_BY_HOST_SECTION["bedrock"] == frozenset()
