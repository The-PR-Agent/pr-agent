"""litellm.model_id belongs to config.model, not to fallback models."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler

PRIMARY = "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0"
FALLBACK = "bedrock/qwen.qwen3-235b-a22b-2507-v1:0"
PROFILE_ARN = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"


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
    def __init__(self, model):
        self.config = _Box(
            reasoning_effort=None,
            ai_timeout=30,
            custom_reasoning_model=False,
            max_model_tokens=32000,
            verbosity_level=0,
            model=model,
        )
        self.litellm = _Box()
        self._values = {
            "litellm.model_id": PROFILE_ARN,
            "aws.AWS_ACCESS_KEY_ID": "test-access-key",
            "aws.AWS_SECRET_ACCESS_KEY": "test-secret-key",
            "aws.AWS_REGION_NAME": "us-east-1",
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


@pytest.fixture
def handler(monkeypatch):
    settings = _Settings(model=PRIMARY)
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    return litellm_handler.LiteLLMAIHandler()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "expected_model_id"),
    [(PRIMARY, PROFILE_ARN), (FALLBACK, None)],
)
async def test_chat_completion_sends_model_id_only_for_config_model(handler, model, expected_model_id):
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        await handler.chat_completion(model=model, system="sys", user="usr")

    kwargs = mock_call.call_args.kwargs
    if expected_model_id is None:
        assert "model_id" not in kwargs
    else:
        assert kwargs["model_id"] == expected_model_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "expected_model_id"),
    [(PRIMARY, PROFILE_ARN), (FALLBACK, None)],
)
async def test_health_probe_sends_model_id_only_for_config_model(handler, model, expected_model_id):
    completion = AsyncMock(return_value=_mock_response())
    await handler.probe_completion(model, _completion=completion)

    kwargs = completion.call_args.kwargs
    if expected_model_id is None:
        assert "model_id" not in kwargs
    else:
        assert kwargs["model_id"] == expected_model_id


OTHER = "bedrock/amazon.nova-pro-v1:0"
PRIMARY_ARN = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/primary1"
FALLBACK_ARN = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/fallback1"


def _handler_with_model_ids(monkeypatch, model_ids):
    settings = _Settings(model=PRIMARY)
    settings._values["litellm.model_ids"] = model_ids
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    return litellm_handler.LiteLLMAIHandler()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "expected_model_id"),
    [(PRIMARY, PRIMARY_ARN), (FALLBACK, FALLBACK_ARN)],
)
async def test_chat_completion_sends_each_model_its_own_model_ids_entry(
    monkeypatch, model, expected_model_id
):
    handler = _handler_with_model_ids(monkeypatch, {PRIMARY: PRIMARY_ARN, FALLBACK: FALLBACK_ARN})
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        await handler.chat_completion(model=model, system="sys", user="usr")

    assert mock_call.call_args.kwargs["model_id"] == expected_model_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "expected_model_id"),
    [(PRIMARY, PROFILE_ARN), (FALLBACK, FALLBACK_ARN), (OTHER, None)],
)
async def test_model_ids_entry_does_not_change_single_model_id_for_config_model(
    monkeypatch, model, expected_model_id
):
    # List only the fallback in model_ids. config.model keeps litellm.model_id,
    # and a model in neither place gets no model_id.
    handler = _handler_with_model_ids(monkeypatch, {FALLBACK: FALLBACK_ARN})
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        await handler.chat_completion(model=model, system="sys", user="usr")

    kwargs = mock_call.call_args.kwargs
    if expected_model_id is None:
        assert "model_id" not in kwargs
    else:
        assert kwargs["model_id"] == expected_model_id


@pytest.mark.asyncio
async def test_health_probe_sends_fallback_its_model_ids_entry(monkeypatch):
    handler = _handler_with_model_ids(monkeypatch, {FALLBACK: FALLBACK_ARN})
    completion = AsyncMock(return_value=_mock_response())

    await handler.probe_completion(FALLBACK, _completion=completion)

    assert completion.call_args.kwargs["model_id"] == FALLBACK_ARN


@pytest.mark.asyncio
async def test_fallback_region_is_not_taken_from_primary_model_id(monkeypatch):
    # Resolve the AWS region per model: a fallback must not pick up the region
    # of the primary's inference profile ARN.
    eu_arn = "arn:aws:bedrock:eu-west-1:123456789012:application-inference-profile/eu1"
    fallback = "bedrock/anthropic.claude-3-haiku-20240307-v1:0"
    settings = _Settings(model=PRIMARY)
    settings._values["litellm.model_id"] = eu_arn
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "request-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "request-secret")
    handler = litellm_handler.LiteLLMAIHandler()

    primary_completion = AsyncMock(return_value=_mock_response())
    await handler.probe_completion(PRIMARY, _completion=primary_completion)
    fallback_completion = AsyncMock(return_value=_mock_response())
    await handler.probe_completion(fallback, _completion=fallback_completion)

    assert primary_completion.call_args.kwargs["aws_region_name"] == "eu-west-1"
    assert fallback_completion.call_args.kwargs.get("aws_region_name") != "eu-west-1"
