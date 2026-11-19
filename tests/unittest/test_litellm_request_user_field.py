"""
Tests for the optional provider-side request attribution in
LiteLLMAIHandler.chat_completion.

When config.add_user_to_requests is enabled, the current command and PR URL
(read from the logging context set by PRAgent.handle_request) are sent in the
OpenAI-compatible "user" request field as a compact JSON string. For
"openrouter/..." models the value is also placed in extra_body, because
LiteLLM's OpenRouter transformation does not forward the standard "user"
parameter. Disabled (the default), the block is a no-op.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from pr_agent.log import get_logger

PR_URL = "https://gitlab.example.com/group/project/-/merge_requests/171"


def _make_settings(add_user_to_requests, config_values=None, litellm_values=None):
    config_values = {"add_user_to_requests": add_user_to_requests, **(config_values or {})}
    litellm_values = litellm_values or {}

    def config_get(self, key, default=None):
        return config_values.get(key, default)

    return type("Settings", (), {
        "config": type("Config", (), {
            "ai_timeout": 30,
            "custom_reasoning_model": config_values.get("custom_reasoning_model", False),
            "max_model_tokens": 32000,
            "verbosity_level": 0,
            "seed": -1,
            "reasoning_effort": None,
            "get": config_get,
        })(),
        "litellm": type("LiteLLM", (), {
            "get": lambda self, key, default=None: litellm_values.get(key, default),
        })(),
        "get": lambda self, key, default=None: default,
    })()


def _mock_response():
    mock = MagicMock()
    response = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    mock.__getitem__.side_effect = response.__getitem__
    mock.dict.return_value = response
    return mock


async def _run(monkeypatch, model, add_user_to_requests, log_context=None, temperature=0.2, **settings):
    monkeypatch.setattr(litellm_handler, "get_settings",
                        lambda: _make_settings(add_user_to_requests, **settings))
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
               new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        if log_context:
            with get_logger().contextualize(**log_context):
                await handler.chat_completion(model=model, system="sys", user="usr", temperature=temperature)
        else:
            await handler.chat_completion(model=model, system="sys", user="usr", temperature=temperature)
    return mock_call.call_args[1]


class TestRequestUserField:

    @pytest.mark.asyncio
    async def test_disabled_by_default_is_noop(self, monkeypatch):
        kwargs = await _run(monkeypatch, "gpt-4o", False,
                            log_context={"command": "improve", "pr_url": PR_URL})
        assert "user" not in kwargs
        assert "extra_body" not in kwargs

    @pytest.mark.asyncio
    async def test_enabled_sets_user_field(self, monkeypatch):
        kwargs = await _run(monkeypatch, "gpt-4o", True,
                            log_context={"command": "improve", "pr_url": PR_URL})
        payload = json.loads(kwargs["user"])
        assert payload == {"command": "improve", "pr_url": PR_URL}
        assert "extra_body" not in kwargs


    @pytest.mark.asyncio
    async def test_enabled_without_context_is_noop(self, monkeypatch):
        kwargs = await _run(monkeypatch, "gpt-4o", True)
        assert "user" not in kwargs
        assert "extra_body" not in kwargs

    @pytest.mark.asyncio
    async def test_openrouter_uses_extra_body_not_user_kwarg(self, monkeypatch):
        # LiteLLM does not list "user" among OpenRouter's supported params, so the
        # value must travel in extra_body only.
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-4.5", True,
                            log_context={"command": "review", "pr_url": PR_URL})
        assert "user" not in kwargs
        assert json.loads(kwargs["extra_body"]["user"])["command"] == "review"

    @pytest.mark.asyncio
    async def test_long_pr_url_keeps_valid_json_under_cap(self, monkeypatch):
        long_url = "https://gitlab.example.com/group/" + "x" * 400 + "/-/merge_requests/9"
        kwargs = await _run(monkeypatch, "gpt-4o", True,
                            log_context={"command": "improve", "pr_url": long_url})
        assert len(kwargs["user"]) <= 256
        payload = json.loads(kwargs["user"])
        assert payload["command"] == "improve"
        assert long_url.startswith(payload["pr_url"])

    @pytest.mark.asyncio
    async def test_concurrent_requests_do_not_cross_attribute(self, monkeypatch):
        import asyncio
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(True))
        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
                   new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _mock_response()
            handler = litellm_handler.LiteLLMAIHandler()

            async def run(command, pr_url):
                with get_logger().contextualize(command=command, pr_url=pr_url):
                    await handler.chat_completion(model="gpt-4o", system="sys", user="usr")

            await asyncio.gather(
                run("improve", "https://gitlab.example.com/a/-/merge_requests/1"),
                run("review", "https://gitlab.example.com/b/-/merge_requests/2"),
            )
        payloads = [json.loads(c.kwargs["user"]) for c in mock_call.call_args_list]
        by_command = {p["command"]: p["pr_url"] for p in payloads}
        assert by_command["improve"].endswith("/a/-/merge_requests/1")
        assert by_command["review"].endswith("/b/-/merge_requests/2")

    @pytest.mark.asyncio
    async def test_unsupported_provider_is_skipped(self, monkeypatch):
        # gemini's parameter mapping does not accept "user": the field is skipped
        # instead of breaking the call when litellm.drop_params is off.
        kwargs = await _run(monkeypatch, "gemini/gemini-2.5-pro", True,
                            log_context={"command": "improve", "pr_url": PR_URL})
        assert "user" not in kwargs
        assert "extra_body" not in kwargs


class TestStringFalseFlags:
    # Dynaconf leaves "FALSE" or a quoted "false" as a str, so plain truthiness reads it as on

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["false", "FALSE"])
    async def test_add_user_to_requests_string_false_is_off(self, monkeypatch, value):
        kwargs = await _run(monkeypatch, "gpt-4o", value,
                            log_context={"command": "improve", "pr_url": PR_URL})
        assert "user" not in kwargs
        assert "extra_body" not in kwargs

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["false", "FALSE"])
    async def test_enable_callbacks_string_false_is_off(self, monkeypatch, value):
        add_callbacks = MagicMock(side_effect=lambda kwargs: kwargs)
        monkeypatch.setattr(litellm_handler.LiteLLMAIHandler, "add_litellm_callbacks",
                            lambda self, kwargs: add_callbacks(kwargs))
        await _run(monkeypatch, "gpt-4o", False, litellm_values={"enable_callbacks": value})
        add_callbacks.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["false", "FALSE"])
    async def test_add_session_id_string_false_is_off(self, monkeypatch, value):
        kwargs = await _run(monkeypatch, "gpt-4o", False,
                            log_context={"command": "improve", "pr_url": PR_URL},
                            litellm_values={"add_session_id_to_requests": value})
        assert "litellm_session_id" not in kwargs
        assert "x-litellm-session-id" not in (kwargs.get("headers") or {})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["false", "FALSE"])
    async def test_custom_reasoning_model_string_false_keeps_system_message(self, monkeypatch, value):
        kwargs = await _run(monkeypatch, "gpt-4o", False,
                            config_values={"custom_reasoning_model": value})
        assert kwargs["messages"][0]["role"] == "system"
        assert "temperature" in kwargs

    @pytest.mark.asyncio
    async def test_add_user_to_requests_string_true_still_works(self, monkeypatch):
        kwargs = await _run(monkeypatch, "gpt-4o", "true",
                            log_context={"command": "improve", "pr_url": PR_URL})
        assert json.loads(kwargs["user"])["command"] == "improve"


class TestSeedSetting:

    @pytest.mark.asyncio
    async def test_numeric_string_seed_is_sent_as_int(self, monkeypatch):
        kwargs = await _run(monkeypatch, "gpt-4o", False, temperature=0,
                            config_values={"seed": "42"})
        assert kwargs["seed"] == 42

    @pytest.mark.asyncio
    async def test_invalid_seed_is_ignored(self, monkeypatch):
        kwargs = await _run(monkeypatch, "gpt-4o", False, temperature=0,
                            config_values={"seed": "abc"})
        assert "seed" not in kwargs

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [True, -0.5, 3.7])
    async def test_bool_or_fractional_seed_is_ignored(self, monkeypatch, value):
        # int() would quietly turn these into 1, 0 or 3 and switch a fixed seed on
        kwargs = await _run(monkeypatch, "gpt-4o", False, temperature=0,
                            config_values={"seed": value})
        assert "seed" not in kwargs
