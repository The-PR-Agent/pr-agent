"""A slow sink must not stall the event loop of a running server.

A webhook, Slack or Telegram sink is an HTTP call with a five second timeout. When
that call runs inline in a tool, the whole deployment waits for it: one slow
collector stops every other pull request the server is handling.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from pr_agent.algo import output_sinks
from pr_agent.algo.run_output import push_outputs_async
from pr_agent.config_loader import get_settings
from pr_agent.tools import pr_code_suggestions, pr_description, pr_reviewer

SINK_URL = 'https://example.test/webhook'


@pytest.fixture(autouse=True)
def _reset_push_outputs():
    # These tests mutate the global settings singleton; restore to disabled afterwards
    # so state can't leak into other test modules.
    yield
    s = get_settings()
    s.set('PUSH_OUTPUTS.ENABLE', False)
    s.set('PUSH_OUTPUTS.CHANNELS', [])
    s.set('PUSH_OUTPUTS.WEBHOOK_URL', '')


def _enable_slow_webhook(monkeypatch, delay=0.3):
    """Point the webhook channel at a collector that answers slowly."""
    get_settings().set('PUSH_OUTPUTS.ENABLE', True)
    get_settings().set('PUSH_OUTPUTS.CHANNELS', ['webhook'])
    get_settings().set('PUSH_OUTPUTS.WEBHOOK_URL', SINK_URL)
    posted = []

    def slow_post(url, **kwargs):
        time.sleep(delay)
        posted.append(url)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(output_sinks.requests, 'post', slow_post)
    return posted


async def test_a_slow_sink_does_not_block_the_event_loop(monkeypatch):
    """The tools run inside an event loop, so the delivery has to leave it free."""
    _enable_slow_webhook(monkeypatch)
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    try:
        await push_outputs_async("review", payload={"a": 1}, markdown="hi")
    finally:
        beat.cancel()

    assert ticks > 1, "the sink delivery blocked the event loop"


async def test_the_output_reaches_the_sink_before_the_caller_resumes(monkeypatch):
    """Waiting for the delivery keeps the guarantee that nothing is lost on exit."""
    posted = _enable_slow_webhook(monkeypatch)

    await push_outputs_async("review", payload={"a": 1}, markdown="hi")

    assert posted == [SINK_URL]


def test_the_tools_do_not_import_the_blocking_delivery():
    """The tools run in an event loop, so they must not call the blocking entry point."""
    for module in (pr_description, pr_code_suggestions, pr_reviewer):
        assert "push_outputs" not in vars(module), f"{module.__name__} imports the blocking delivery"
