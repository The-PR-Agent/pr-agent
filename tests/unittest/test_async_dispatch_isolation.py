import asyncio
import contextvars
import threading

import pytest

import pr_agent.agent.pr_agent as pr_agent_module
from pr_agent.algo.run_details import get_run_details, init_run_details, scoped_run_details

_request_label = contextvars.ContextVar("test_request_label", default=None)


class _DeferrableTool:
    """Records which thread and RunDetails each lifecycle phase saw."""

    seen: dict = {}

    def __init__(self, pr_url, ai_handler=None, args=None, *, _defer_prepare=False):
        self.pr_url = pr_url
        assert _defer_prepare is True

    def prepare(self):
        details = get_run_details()
        details.model_used = self.pr_url
        self.seen[self.pr_url] = {"prepare_thread": threading.get_ident(), "details": details}

    async def run(self):
        entry = self.seen[self.pr_url]
        entry["run_details"] = get_run_details()
        entry["run_thread"] = threading.get_ident()
        return True


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(pr_agent_module, "enforce_request_policy", lambda _url: None)
    monkeypatch.setitem(pr_agent_module.command2class, "isolated", _DeferrableTool)
    _DeferrableTool.seen = {}
    return pr_agent_module.PRAgent(ai_handler="fake-ai")


async def test_concurrent_requests_isolate_context_across_worker_threads(monkeypatch, agent):
    # Both calls must be inside apply_repo_settings at once, which only works off the event loop.
    barrier = threading.Barrier(2, timeout=5)
    settings_calls = {}

    def fake_apply_repo_settings(pr_url):
        settings_calls[pr_url] = (threading.get_ident(), _request_label.get())
        barrier.wait()

    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", fake_apply_repo_settings)

    async def request(pr_url):
        _request_label.set(pr_url)
        return await agent.handle_request(pr_url, "/isolated")

    loop_thread = threading.get_ident()
    urls = ["https://example/pr/1", "https://example/pr/2"]
    assert await asyncio.gather(*(request(url) for url in urls)) == [True, True]

    for url in urls:
        thread, label = settings_calls[url]
        assert thread != loop_thread
        assert label == url
        seen = _DeferrableTool.seen[url]
        assert seen["prepare_thread"] != loop_thread
        assert seen["run_thread"] == loop_thread
        assert seen["run_details"] is seen["details"]
        assert seen["details"].model_used == url
    assert _DeferrableTool.seen[urls[0]]["details"] is not _DeferrableTool.seen[urls[1]]["details"]


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


def test_scoped_run_details_isolates_and_resets():
    def check():
        assert get_run_details() is None
        with scoped_run_details() as only:
            assert get_run_details() is only
        assert get_run_details() is None

        outer = init_run_details()
        with scoped_run_details() as inner:
            assert inner is not outer
            assert get_run_details() is inner
            inner.num_ai_calls = 1
        assert get_run_details() is outer
        assert outer.num_ai_calls == 0

        with pytest.raises(RuntimeError), scoped_run_details():
            raise RuntimeError("boom")
        assert get_run_details() is outer

    contextvars.Context().run(check)
