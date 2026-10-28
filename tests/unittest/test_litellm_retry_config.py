"""Unit tests for the retry-configuration knobs in litellm_ai_handler.

Covers config.retry_same_model_on_timeout and config.num_retries: defaults preserve the
previous behavior, env-style string values parse the way an operator expects (a workflow
env override arrives as the string "false", which plain bool() would read as truthy), and a
malformed num_retries is ignored rather than raised — it is read on the request path, where
an escaping ValueError would be wrapped and retried as an API error.
"""

import httpx
import litellm
import openai
import pytest
from litellm.exceptions import MidStreamFallbackError
from tenacity import RetryCallState

from pr_agent.algo.ai_handlers.litellm_ai_handler import (
    DEFAULT_NUM_RETRIES,
    MODEL_RETRIES,
    EmptyTruncatedResponseError,
    _as_bool,
    _configured_client_retries,
    _is_rejected_request,
    _retry_after_seconds,
    _same_model_retry_wait,
    _should_retry_same_model,
    _stop_same_model_retries,
)
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

KEYS = (
    "config.retry_same_model_on_timeout",
    "config.retry_same_model_on_length",
    "config.num_retries",
)


@pytest.fixture(autouse=True)
def _isolate_settings():
    snap = snapshot_settings(KEYS)
    yield
    restore_settings(snap)


def _timeout_error():
    return openai.APITimeoutError(request=httpx.Request("POST", "http://model.invalid"))


def _api_error():
    return openai.APIError("boom", request=httpx.Request("POST", "http://model.invalid"), body=None)


def _empty_truncated_error():
    return EmptyTruncatedResponseError(
        "Empty content in model response (finish_reason: length)",
        request=httpx.Request("POST", "http://model.invalid"),
        body=None,
    )


def _answered(error, headers=None):
    # LiteLLM attaches the provider's response headers to an error the provider answered.
    error.litellm_response_headers = httpx.Headers(headers or {})
    return error


def _rate_limit_error(headers=None):
    return _answered(litellm.RateLimitError("throttled", llm_provider="bedrock", model="m"), headers)


def _connection_error(cause):
    error = litellm.APIConnectionError("no answer", llm_provider="bedrock", model="m")
    error.__cause__ = cause
    return error


def _retry_state(error, attempt_number=1):
    state = RetryCallState(retry_object=None, fn=None, args=(), kwargs={})
    state.attempt_number = attempt_number
    state.set_exception((type(error), error, None))
    return state


class TestShouldRetrySameModel:
    def test_default_retries_timeouts(self):
        # Preserves pre-knob behavior: an unset flag keeps same-model retries on timeout.
        assert _should_retry_same_model(_timeout_error()) is True

    @pytest.mark.parametrize("value", [False, 0, "false", "False", "0", "no", "off", "", "invalid"])
    def test_disabled_hands_timeout_to_fallback(self, value):
        get_settings().set("config.retry_same_model_on_timeout", value)
        assert _should_retry_same_model(_timeout_error()) is False

    @pytest.mark.parametrize("value", [True, 1, "true", "TRUE", "1", "yes", "on"])
    def test_enabled_keeps_retrying(self, value):
        get_settings().set("config.retry_same_model_on_timeout", value)
        assert _should_retry_same_model(_timeout_error()) is True

    def test_rate_limit_retries_same_model(self):
        err = openai.RateLimitError(
            "slow down", response=httpx.Response(429, request=httpx.Request("POST", "http://model.invalid")), body=None
        )
        assert _should_retry_same_model(err) is True

    def test_other_api_errors_still_retry(self):
        assert _should_retry_same_model(_api_error()) is True

    def test_length_truncation_does_not_retry_same_model_by_default(self):
        # An empty, length-capped response is deterministic for the same request and cap.
        assert _should_retry_same_model(_empty_truncated_error()) is False

    @pytest.mark.parametrize("value", [True, 1, "true", "TRUE", "1", "yes", "on"])
    def test_length_truncation_retries_when_enabled(self, value):
        get_settings().set("config.retry_same_model_on_length", value)
        assert _should_retry_same_model(_empty_truncated_error()) is True

    @pytest.mark.parametrize("value", [False, 0, "false", "False", "0", "no", "off"])
    def test_length_truncation_hands_to_fallback_when_disabled(self, value):
        get_settings().set("config.retry_same_model_on_length", value)
        assert _should_retry_same_model(_empty_truncated_error()) is False

    def test_empty_truncated_error_is_an_api_error(self):
        # retry_with_fallback_models advances to the next model only for openai.APIError subclasses.
        assert isinstance(_empty_truncated_error(), openai.APIError)

    @pytest.mark.parametrize("error_type,status", [
        (openai.BadRequestError, 400),
        (openai.UnprocessableEntityError, 422),
    ])
    def test_request_validation_errors_do_not_retry(self, error_type, status):
        error = error_type(
            "invalid request",
            response=httpx.Response(status, request=httpx.Request("POST", "http://model.invalid")),
            body=None,
        )
        assert _should_retry_same_model(error) is False

    @pytest.mark.parametrize("error_type", [
        litellm.BadRequestError,
        litellm.ContextWindowExceededError,
        litellm.ContentPolicyViolationError,
    ])
    def test_litellm_bad_request_subtypes_do_not_retry(self, error_type):
        error = error_type("rejected request", model="gpt-4o", llm_provider="openai")
        assert _should_retry_same_model(error) is False

    def test_non_api_errors_never_retry(self):
        assert _should_retry_same_model(ValueError("not an API error")) is False


class TestAsBool:
    def test_non_string_non_bool_falls_back_to_default(self):
        assert _as_bool(object(), default=True) is True
        assert _as_bool(None, default=False) is False


class TestConfiguredClientRetries:
    def test_shipped_default(self):
        assert _configured_client_retries() == DEFAULT_NUM_RETRIES

    def test_unset_means_handler_default(self):
        get_settings().set("config.num_retries", None)
        assert _configured_client_retries() == DEFAULT_NUM_RETRIES

    @pytest.mark.parametrize("value,expected", [(0, 0), (2, 2), ("0", 0), (" 3 ", 3)])
    def test_valid_values_parse(self, value, expected):
        get_settings().set("config.num_retries", value)
        assert _configured_client_retries() == expected

    @pytest.mark.parametrize("value", ["abc", "1.5", "", -1, "-2"])
    def test_invalid_values_are_ignored_not_raised(self, value):
        get_settings().set("config.num_retries", value)
        assert _configured_client_retries() == DEFAULT_NUM_RETRIES


class TestIsRejectedRequest:
    @pytest.mark.parametrize("error", [
        pytest.param(_rate_limit_error(), id="429"),
        pytest.param(_answered(litellm.ServiceUnavailableError("down", llm_provider="bedrock", model="m")), id="503"),
        pytest.param(_answered(litellm.InternalServerError("boom", llm_provider="openai", model="m")), id="500"),
        pytest.param(_answered(litellm.APIError(529, "overloaded", llm_provider="anthropic", model="m")), id="529"),
        pytest.param(_connection_error(httpx.ConnectError("refused")), id="connection-error"),
    ])
    def test_rejected(self, error):
        assert _is_rejected_request(error) is True

    @pytest.mark.parametrize("error", [
        # A 200 that fails to parse: LiteLLM reports a 5xx or a connection error with no answer behind it.
        pytest.param(
            litellm.InternalServerError("Expecting value", llm_provider="openai", model="m"), id="unparsed-500",
        ),
        pytest.param(_connection_error(KeyError("output")), id="unparsed-connection-error"),
        pytest.param(_connection_error(httpx.ReadTimeout("slow")), id="transport-timeout"),
        pytest.param(_timeout_error(), id="timeout"),
        pytest.param(_answered(MidStreamFallbackError("cut off", model="m", llm_provider="openai")), id="mid-stream"),
        pytest.param(_empty_truncated_error(), id="empty-truncated"),
        pytest.param(litellm.BadRequestError("bad", model="m", llm_provider="openai"), id="400"),
        pytest.param(ValueError("not an API error"), id="not-an-api-error"),
    ])
    def test_not_rejected(self, error):
        assert _is_rejected_request(error) is False


class TestRetryAfterSeconds:
    @pytest.mark.parametrize("value,expected", [("7", 7.0), ("0.5", 0.5), (" 30 ", 30.0)])
    def test_honored(self, value, expected):
        assert _retry_after_seconds(_rate_limit_error({"Retry-After": value})) == expected

    @pytest.mark.parametrize("value", [
        "nan", "inf", "1e400", "-1", "0", "31", "86400", "soon", "Wed, 21 Oct 2026 07:28:00 GMT",
    ])
    def test_ignored(self, value):
        assert _retry_after_seconds(_rate_limit_error({"retry-after": value})) is None

    def test_missing(self):
        assert _retry_after_seconds(_rate_limit_error()) is None
        assert _retry_after_seconds(litellm.RateLimitError("throttled", llm_provider="openai", model="m")) is None


class TestSameModelRetrySchedule:
    def test_rejected_request_waits_retry_after(self):
        assert _same_model_retry_wait(_retry_state(_rate_limit_error({"retry-after": "7"}))) == 7.0

    @pytest.mark.parametrize("value", [None, "nan", "-1", "0", "3600"])
    def test_rejected_request_without_usable_retry_after_waits_with_jitter(self, value):
        headers = {"retry-after": value} if value else None
        for attempt in range(1, 6):
            wait = _same_model_retry_wait(_retry_state(_rate_limit_error(headers), attempt))
            assert 0 <= wait <= min(20, 2 ** (attempt - 1))

    def test_timeout_is_replayed_at_once(self):
        assert _same_model_retry_wait(_retry_state(_timeout_error())) == 0

    @pytest.mark.parametrize("num_retries,expected_attempts", [(None, DEFAULT_NUM_RETRIES + 1), (0, 1), (2, 3)])
    def test_rejected_request_stops_after_num_retries(self, num_retries, expected_attempts):
        get_settings().set("config.num_retries", num_retries)
        stops = [_stop_same_model_retries(_retry_state(_rate_limit_error(), n)) for n in range(1, 8)]
        assert stops.index(True) + 1 == expected_attempts

    @pytest.mark.parametrize("num_retries", [0, 7])
    def test_timeout_keeps_model_retries(self, num_retries):
        get_settings().set("config.num_retries", num_retries)
        stops = [_stop_same_model_retries(_retry_state(_timeout_error(), n)) for n in range(1, 8)]
        assert stops.index(True) + 1 == MODEL_RETRIES
