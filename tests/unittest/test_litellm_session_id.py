"""Tests for the optional LiteLLM session id header.

When litellm.add_session_id_to_requests is enabled, every request carries a
stable x-litellm-session-id derived from the PR URL, so LiteLLM groups all
commands run against the same PR into one session. Disabled (the default), the
header is not added.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from pr_agent.log import get_logger

PR_URL = "https://github.com/owner/repo/pull/123"


def _make_settings(add_session_id_to_requests, extra_headers=None):
    def config_get(self, key, default=None):
        return default

    def litellm_get(self, key, default=None):
        if key == "add_session_id_to_requests":
            return add_session_id_to_requests
        return default

    def settings_get(self, key, default=None):
        if key == "LITELLM.EXTRA_HEADERS":
            return extra_headers
        return default

    return type("Settings", (), {
        "config": type("Config", (), {
            "ai_timeout": 30,
            "custom_reasoning_model": False,
            "max_model_tokens": 32000,
            "verbosity_level": 0,
            "seed": -1,
            "reasoning_effort": None,
            "get": config_get,
        })(),
        "litellm": type("LiteLLM", (), {"get": litellm_get})(),
        "get": settings_get,
    })()


def _mock_response():
    mock = MagicMock()
    response = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    mock.__getitem__.side_effect = response.__getitem__
    mock.dict.return_value = response
    return mock


async def _run(monkeypatch, add_session_id_to_requests, log_context=None, extra_headers=None):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings(add_session_id_to_requests, extra_headers),
    )
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
               new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        if log_context:
            with get_logger().contextualize(**log_context):
                await handler.chat_completion(model="gpt-4o", system="sys", user="usr")
        else:
            await handler.chat_completion(model="gpt-4o", system="sys", user="usr")
    return mock_call.call_args[1]


class TestSessionIdHeader:

    @pytest.mark.asyncio
    async def test_disabled_by_default_is_noop(self, monkeypatch):
        kwargs = await _run(monkeypatch, False, log_context={"command": "improve", "pr_url": PR_URL})
        assert "x-litellm-session-id" not in (kwargs.get("headers") or {})

    @pytest.mark.asyncio
    async def test_enabled_sets_stable_header(self, monkeypatch):
        kwargs = await _run(monkeypatch, True, log_context={"command": "improve", "pr_url": PR_URL})
        assert kwargs["headers"]["x-litellm-session-id"] == (
            "pr-agent-github-com-owner-repo-pull-123-b065a0b1d1c7b721"
        )

    @pytest.mark.asyncio
    async def test_url_credentials_are_not_in_session_id(self, monkeypatch):
        credential_url = "https://user:secret-token@github.com/owner/repo/pull/123"
        clean = await _run(monkeypatch, True, log_context={"command": "review", "pr_url": PR_URL})
        with_credentials = await _run(
            monkeypatch, True, log_context={"command": "review", "pr_url": credential_url},
        )
        clean_id = clean["headers"]["x-litellm-session-id"]
        credential_id = with_credentials["headers"]["x-litellm-session-id"]
        assert credential_id == clean_id
        assert "user" not in credential_id
        assert "secret-token" not in credential_id

    @pytest.mark.asyncio
    async def test_same_pr_shares_session_across_commands(self, monkeypatch):
        first = await _run(monkeypatch, True, log_context={"command": "improve", "pr_url": PR_URL})
        second = await _run(monkeypatch, True, log_context={"command": "review", "pr_url": PR_URL})
        assert first["headers"]["x-litellm-session-id"] == second["headers"]["x-litellm-session-id"]

    @pytest.mark.asyncio
    async def test_different_prs_get_different_sessions(self, monkeypatch):
        first = await _run(monkeypatch, True, log_context={"command": "review", "pr_url": PR_URL})
        second = await _run(monkeypatch, True, log_context={
            "command": "review", "pr_url": "https://github.com/owner/repo/pull/124",
        })
        assert first["headers"]["x-litellm-session-id"] != second["headers"]["x-litellm-session-id"]

    @pytest.mark.asyncio
    async def test_enabled_without_context_is_noop(self, monkeypatch):
        kwargs = await _run(monkeypatch, True)
        assert "x-litellm-session-id" not in (kwargs.get("headers") or {})

    @pytest.mark.asyncio
    async def test_value_uses_litellm_allowed_characters(self, monkeypatch):
        pr_url = "https://gitlab.example.com/group/project/-/merge_requests/171?foo=bar"
        kwargs = await _run(monkeypatch, True, log_context={"command": "improve", "pr_url": pr_url})
        session_id = kwargs["headers"]["x-litellm-session-id"]
        assert session_id == (
            "pr-agent-gitlab-example-com-group-project-merge_requests-171-4534b7cf8d89d676"
        )
        assert len(session_id) >= 8

    @pytest.mark.asyncio
    async def test_punctuation_differences_do_not_collide(self, monkeypatch):
        first = await _run(monkeypatch, True, log_context={
            "command": "review", "pr_url": "https://github.com/owner/repo/pull/1",
        })
        second = await _run(monkeypatch, True, log_context={
            "command": "review", "pr_url": "https://github.com/owner/repo/pull-1",
        })
        first_id = first["headers"]["x-litellm-session-id"]
        second_id = second["headers"]["x-litellm-session-id"]
        assert first_id.rsplit("-", 1)[0] == second_id.rsplit("-", 1)[0]
        assert first_id != second_id

    @pytest.mark.asyncio
    async def test_long_paths_do_not_collide(self, monkeypatch):
        long_path = "https://github.com/" + "a" * 200 + "/pull/"
        first = await _run(monkeypatch, True, log_context={
            "command": "review", "pr_url": long_path + "1",
        })
        second = await _run(monkeypatch, True, log_context={
            "command": "review", "pr_url": long_path + "2",
        })
        first_id = first["headers"]["x-litellm-session-id"]
        second_id = second["headers"]["x-litellm-session-id"]
        assert first_id != second_id
        assert len(first_id) <= 128
        assert len(second_id) <= 128

    @pytest.mark.asyncio
    async def test_explicit_header_is_preserved(self, monkeypatch):
        kwargs = await _run(
            monkeypatch,
            True,
            log_context={"command": "improve", "pr_url": PR_URL},
            extra_headers='{"x-litellm-session-id": "operator-session"}',
        )
        assert kwargs["headers"]["x-litellm-session-id"] == "operator-session"
