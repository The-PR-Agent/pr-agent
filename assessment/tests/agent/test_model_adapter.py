from __future__ import annotations

import time

import httpx
import pytest

from assessment.ai_reviewer.model_adapter import (
    ModelConfig,
    ModelConfigurationError,
    OpenAICompatibleModel,
)


def test_missing_key_fails_without_echoing_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AI_REVIEW_API_KEY", raising=False)
    monkeypatch.delenv("GLM_API_KEY", raising=False)

    with pytest.raises(ModelConfigurationError) as captured:
        ModelConfig.from_env()

    assert "required" in str(captured.value)


@pytest.mark.parametrize(
    ("base", "endpoint"),
    (
        (
            "https://api.example.test",
            "https://api.example.test/v1/chat/completions",
        ),
        (
            "https://api.example.test/v1",
            "https://api.example.test/v1/chat/completions",
        ),
    ),
)
def test_endpoint_normalization(base: str, endpoint: str) -> None:
    config = ModelConfig(api_base=base, api_key="test-key", model="model")

    assert config.endpoint == endpoint


def test_direct_openai_compatible_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            headers={"x-request-id": "request-17"},
            json={
                "model": "deepseek-v4-pro",
                "choices": [
                    {"message": {"content": '{"findings": []}'}}
                ],
                "usage": {"total_tokens": 12},
            },
        )

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleModel(
        ModelConfig(
            api_base="https://api.example.test",
            api_key="test-key",
            model="deepseek-v4-pro",
        ),
        client_factory=lambda **kwargs: httpx.Client(
            transport=transport,
            **kwargs,
        ),
    )

    response = client.complete(
        [{"role": "user", "content": "return json"}],
        time.monotonic() + 5,
    )

    assert response.model == "deepseek-v4-pro"
    assert response.request_id == "request-17"
    assert response.usage == {"total_tokens": 12}
