"""Token counting from inside a running event loop must not hop to a worker thread or loop."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.algo import token_handler
from tests.unittest.test_token_handler import _handler, _patch_acount_tokens, _settings


@pytest.fixture
def no_worker_loop(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("token counting created a worker thread or event loop")

    monkeypatch.setattr(token_handler, "ThreadPoolExecutor", forbidden)
    monkeypatch.setattr(token_handler.asyncio, "new_event_loop", forbidden)


def test_encoder_and_default_count_inside_running_loop_match_sync_results(monkeypatch, no_worker_loop):
    monkeypatch.setattr(token_handler, "get_settings", lambda use_context=False: _settings(model="gpt-4o"))
    text = "def add(a, b):\n    return a + b\n"
    sync_count = token_handler.TokenHandler(model="gpt-4o").count_tokens(text)

    async def inside_loop():
        encoder = token_handler.TokenEncoder.get_token_encoder("gpt-4o")
        handler = token_handler.TokenHandler(model="gpt-4o")
        return len(encoder.encode(text)), handler.count_tokens(text), await handler.acount_tokens(text)

    assert asyncio.run(inside_loop()) == (sync_count, sync_count, sync_count)
    assert sync_count > 0


async def test_acount_tokens_awaits_provider_count_on_the_running_loop(monkeypatch, no_worker_loop):
    monkeypatch.setattr(
        token_handler, "get_settings", lambda use_context=False: _settings(model="claude-sonnet-4-6")
    )
    caller_loop = asyncio.get_running_loop()
    seen_loops = []

    async def acount_tokens(**kwargs):
        seen_loops.append(asyncio.get_running_loop())
        return MagicMock(total_tokens=42, tokenizer_type="anthropic_api")

    mock = _patch_acount_tokens(monkeypatch, AsyncMock(side_effect=acount_tokens))

    assert await _handler("claude-sonnet-4-6").acount_tokens("patch", force_accurate=True) == 42
    assert seen_loops == [caller_loop]
    assert mock.await_args.kwargs["model"] == "claude-sonnet-4-6"


async def test_acount_tokens_applies_estimate_factor_like_sync_path(monkeypatch, no_worker_loop):
    monkeypatch.setattr(
        token_handler, "get_settings",
        lambda use_context=False: _settings(model="claude-sonnet-4-6", estimate_factor=0.3),
    )
    _patch_acount_tokens(monkeypatch, AsyncMock(return_value=MagicMock(tokenizer_type="local_tokenizer")))

    assert await _handler("claude-sonnet-4-6").acount_tokens("patch", force_accurate=True) == 13
