import asyncio
import contextvars
import copy
import threading

import pytest
from starlette_context import request_cycle_context

import pr_agent.agent.pr_agent as pr_agent_module
from pr_agent.algo.run_details import get_run_details
from pr_agent.config_loader import get_settings

_request_label = contextvars.ContextVar("test_request_label", default=None)


class _BlockingTool:
    """Record the worker constructor and event-loop run contexts."""

    seen: dict = {}

    def __init__(self, pr_url, ai_handler=None, args=None):
        self.pr_url = pr_url
        details = get_run_details()
        details.model_used = self.pr_url
        self.seen[self.pr_url] = {"init_thread": threading.get_ident(), "details": details}

    async def run(self):
        entry = self.seen[self.pr_url]
        entry["run_details"] = get_run_details()
        entry["run_thread"] = threading.get_ident()
        return True


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(pr_agent_module, "enforce_request_policy", lambda _url: None)
    monkeypatch.setitem(pr_agent_module.command2class, "isolated", _BlockingTool)
    _BlockingTool.seen = {}
    return pr_agent_module.PRAgent(ai_handler="fake-ai")


async def test_concurrent_requests_isolate_context_across_worker_threads(monkeypatch, agent):
    # Both calls must be inside apply_repo_settings at once, which only works off the event loop.
    barrier = threading.Barrier(2, timeout=5)
    settings_calls = {}

    def fake_apply_repo_settings(pr_url):
        settings_calls[pr_url] = (threading.get_ident(), _request_label.get())
        get_settings().config.model = pr_url
        barrier.wait()
        assert get_settings().config.model == pr_url

    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", fake_apply_repo_settings)

    async def request(pr_url):
        _request_label.set(pr_url)
        with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
            return await agent.handle_request(pr_url, "/isolated")

    loop_thread = threading.get_ident()
    urls = ["https://example/pr/1", "https://example/pr/2"]
    assert await asyncio.gather(*(request(url) for url in urls)) == [True, True]

    for url in urls:
        thread, label = settings_calls[url]
        assert thread != loop_thread
        assert label == url
        seen = _BlockingTool.seen[url]
        assert seen["init_thread"] != loop_thread
        assert seen["run_thread"] == loop_thread
        assert seen["run_details"] is seen["details"]
        assert seen["details"].model_used == url
    assert _BlockingTool.seen[urls[0]]["details"] is not _BlockingTool.seen[urls[1]]["details"]


async def test_slow_repo_settings_do_not_block_sibling_coroutine(monkeypatch, agent):
    heartbeat_done = threading.Event()

    def slow_apply_repo_settings(_pr_url):
        # Blocks until the heartbeat ticks; on the event loop this would deadlock until timeout.
        assert heartbeat_done.wait(timeout=5)

    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", slow_apply_repo_settings)

    async def heartbeat():
        for _ in range(3):
            await asyncio.sleep(0.01)
        heartbeat_done.set()

    result, _ = await asyncio.gather(agent.handle_request("https://example/pr/3", "/isolated"), heartbeat())
    assert result is True


async def test_slow_tool_constructor_does_not_block_sibling_coroutine(monkeypatch, agent):
    heartbeat_done = threading.Event()

    class SlowTool(_BlockingTool):
        def __init__(self, *args, **kwargs):
            assert heartbeat_done.wait(timeout=5)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda _url: None)
    monkeypatch.setitem(pr_agent_module.command2class, "isolated", SlowTool)

    async def heartbeat():
        await asyncio.sleep(0)
        heartbeat_done.set()

    result, _ = await asyncio.gather(agent.handle_request("https://example/pr/4", "/isolated"), heartbeat())
    assert result is True
