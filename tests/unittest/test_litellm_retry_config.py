"""Unit tests for the retry-configuration knobs in litellm_ai_handler.

Covers config.retry_same_model_on_timeout and config.num_retries: defaults preserve the
previous behavior, env-style string values parse the way an operator expects (a workflow
env override arrives as the string "false", which plain bool() would read as truthy), and a
malformed num_retries is ignored rather than raised — it is read on the request path, where
an escaping ValueError would be wrapped and retried as an API error.
"""

import httpx
import openai
import pytest

from pr_agent.algo.ai_handlers.litellm_ai_handler import (
    _as_bool,
    _configured_client_retries,
    _should_retry_same_model,
)
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

KEYS = ("config.retry_same_model_on_timeout", "config.num_retries")


@pytest.fixture(autouse=True)
def _isolate_settings():
    snap = snapshot_settings(KEYS)
    yield
    restore_settings(snap)


def _timeout_error():
    return openai.APITimeoutError(request=httpx.Request("POST", "http://model.invalid"))


def _api_error():
    return openai.APIError("boom", request=httpx.Request("POST", "http://model.invalid"), body=None)


class TestShouldRetrySameModel:
    def test_default_retries_timeouts(self):
        # Preserves pre-knob behavior: an unset flag keeps same-model retries on timeout.
        assert _should_retry_same_model(_timeout_error()) is True

    @pytest.mark.parametrize("value", [False, "false", "False", "0", "no", "off"])
    def test_disabled_hands_timeout_to_fallback(self, value):
        get_settings().set("config.retry_same_model_on_timeout", value)
        assert _should_retry_same_model(_timeout_error()) is False

    @pytest.mark.parametrize("value", [True, "true", "TRUE", "1", "yes", "on"])
    def test_enabled_keeps_retrying(self, value):
        get_settings().set("config.retry_same_model_on_timeout", value)
        assert _should_retry_same_model(_timeout_error()) is True

    def test_rate_limit_never_retries_same_model(self):
        err = openai.RateLimitError(
            "slow down", response=httpx.Response(429, request=httpx.Request("POST", "http://model.invalid")), body=None
        )
        assert _should_retry_same_model(err) is False

    def test_other_api_errors_still_retry(self):
        assert _should_retry_same_model(_api_error()) is True

    def test_non_api_errors_never_retry(self):
        assert _should_retry_same_model(ValueError("not an API error")) is False


class TestAsBool:
    def test_non_string_non_bool_falls_back_to_default(self):
        assert _as_bool(object(), default=True) is True
        assert _as_bool(None, default=False) is False


class TestConfiguredClientRetries:
    def test_unset_means_client_defaults(self):
        assert _configured_client_retries() is None

    @pytest.mark.parametrize("value,expected", [(0, 0), (2, 2), ("0", 0), (" 3 ", 3)])
    def test_valid_values_parse(self, value, expected):
        get_settings().set("config.num_retries", value)
        assert _configured_client_retries() == expected

    @pytest.mark.parametrize("value", ["abc", "1.5", "", -1, "-2"])
    def test_invalid_values_are_ignored_not_raised(self, value):
        get_settings().set("config.num_retries", value)
        assert _configured_client_retries() is None
