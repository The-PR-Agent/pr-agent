from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import httpx

DEFAULT_API_BASE = "https://api.ai-native-x.site"
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_TIMEOUT_SECONDS = 90.0


class ModelError(RuntimeError):
    """Base exception for controlled model failures."""


class ModelConfigurationError(ModelError):
    """Raised when required model configuration is absent."""


class ModelTimeout(ModelError):
    """Raised when the model cannot finish inside the analysis deadline."""


@dataclass(frozen=True, slots=True)
class ModelConfig:
    api_base: str
    api_key: str
    model: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = 1

    @classmethod
    def from_env(cls) -> ModelConfig:
        api_key = os.getenv("AI_REVIEW_API_KEY") or os.getenv("GLM_API_KEY")
        if not api_key:
            raise ModelConfigurationError(
                "AI_REVIEW_API_KEY or GLM_API_KEY is required"
            )
        api_base = os.getenv("AI_REVIEW_API_BASE", DEFAULT_API_BASE).strip()
        model = os.getenv("AI_REVIEW_MODEL", DEFAULT_MODEL).strip()
        if not api_base or not model:
            raise ModelConfigurationError(
                "AI_REVIEW_API_BASE and AI_REVIEW_MODEL must not be empty"
            )
        return cls(api_base=api_base, api_key=api_key, model=model)

    @property
    def endpoint(self) -> str:
        base = self.api_base.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    model: str
    request_id: str | None
    usage: Mapping[str, int]


class OpenAICompatibleModel:
    def __init__(
        self,
        config: ModelConfig | None = None,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or ModelConfig.from_env()
        self._client_factory = client_factory
        self._sleep = sleep

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        deadline_monotonic: float,
    ) -> ModelResponse:
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise ModelTimeout(
                    "analysis deadline expired before model call"
                )
            timeout = min(self.config.timeout_seconds, remaining)
            try:
                return self._request(messages, timeout)
            except (httpx.TimeoutException, httpx.TransportError) as error:
                last_error = error
                if attempt >= self.config.max_retries:
                    break
                self._bounded_backoff(deadline_monotonic, attempt)
            except httpx.HTTPStatusError as error:
                last_error = error
                if (
                    attempt >= self.config.max_retries
                    or not _is_retryable_status(error.response.status_code)
                ):
                    break
                self._bounded_backoff(deadline_monotonic, attempt)
        if isinstance(last_error, httpx.TimeoutException):
            raise ModelTimeout("model request timed out") from last_error
        if isinstance(last_error, httpx.HTTPStatusError):
            status = last_error.response.status_code
            request_id = last_error.response.headers.get(
                "x-request-id",
                "unknown",
            )
            raise ModelError(
                f"model API returned HTTP {status}; request_id={request_id}"
            ) from last_error
        raise ModelError("model API transport failed") from last_error

    def _request(
        self,
        messages: Sequence[Mapping[str, str]],
        timeout: float,
    ) -> ModelResponse:
        payload = {
            "model": self.config.model,
            "messages": [dict(message) for message in messages],
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        with self._client_factory(timeout=timeout) as client:
            response = client.post(
                self.config.endpoint,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ModelError(
                "model response is missing message content"
            ) from error
        if not isinstance(content, str) or not content.strip():
            raise ModelError("model response content is empty")
        usage = data.get("usage") or {}
        safe_usage = {
            str(key): int(value)
            for key, value in usage.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        return ModelResponse(
            content=content,
            model=str(data.get("model") or self.config.model),
            request_id=response.headers.get("x-request-id"),
            usage=safe_usage,
        )

    def _bounded_backoff(
        self,
        deadline_monotonic: float,
        attempt: int,
    ) -> None:
        remaining = deadline_monotonic - time.monotonic()
        delay = min(float(2**attempt), max(0.0, remaining))
        if delay > 0:
            self._sleep(delay)


def _is_retryable_status(status_code: int) -> bool:
    return status_code in {408, 409, 429} or status_code >= 500
