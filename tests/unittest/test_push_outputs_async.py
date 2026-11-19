"""Built-in HTTP outputs run concurrently without losing the original delivery wait."""

import asyncio
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from types import SimpleNamespace

import pytest
from starlette_context import request_cycle_context

from pr_agent.algo import output_sinks, run_output
from pr_agent.algo.run_output import push_outputs_async
from pr_agent.commands import pr_description, pr_reviewer
from pr_agent.config_loader import get_settings, global_settings

WEBHOOK_URL = "https://webhook.example.test/secret"
TEAMS_URL = "https://teams.example.test/secret"


@pytest.fixture(autouse=True)
def _reset_push_outputs():
    yield
    settings = get_settings()
    settings.set("PUSH_OUTPUTS.ENABLE", False)
    settings.set("PUSH_OUTPUTS.CHANNELS", [])
    settings.set("PUSH_OUTPUTS.WEBHOOK_URL", "")
    settings.set("PUSH_OUTPUTS.TEAMS_WEBHOOK_URL", "")


def _enable_channels(*channels):
    settings = get_settings()
    settings.set("PUSH_OUTPUTS.ENABLE", True)
    settings.set("PUSH_OUTPUTS.CHANNELS", list(channels))
    settings.set("PUSH_OUTPUTS.WEBHOOK_URL", WEBHOOK_URL)
    settings.set("PUSH_OUTPUTS.TEAMS_WEBHOOK_URL", TEAMS_URL)


async def test_a_slow_sink_does_not_block_the_event_loop(monkeypatch):
    _enable_channels("webhook")
    post_started = threading.Event()
    release_post = threading.Event()
    ticks = 0

    def slow_post(_url, **_kwargs):
        post_started.set()
        assert release_post.wait(2)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(output_sinks.requests, "post", slow_post)

    async def heartbeat():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.005)

    beat = asyncio.create_task(heartbeat())
    delivery = asyncio.create_task(push_outputs_async("review", payload={"a": 1}))
    try:
        assert await asyncio.to_thread(post_started.wait, 2)
        ticks_while_blocked = ticks
        await asyncio.sleep(0.05)
        assert not delivery.done(), "the HTTP send must still be blocked"
        assert ticks > ticks_while_blocked + 1
    finally:
        release_post.set()
        beat.cancel()
        await asyncio.wait_for(delivery, 3)


async def test_async_delivery_uses_request_scoped_settings(monkeypatch):
    delivered = []
    scoped_settings = copy.deepcopy(global_settings)
    scoped_settings.set("PUSH_OUTPUTS.ENABLE", True)
    scoped_settings.set("PUSH_OUTPUTS.CHANNELS", ["webhook"])
    scoped_settings.set("PUSH_OUTPUTS.WEBHOOK_URL", WEBHOOK_URL)

    def fake_post(url, **_kwargs):
        delivered.append(url)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(output_sinks.requests, "post", fake_post)
    with request_cycle_context({"settings": scoped_settings}):
        await push_outputs_async("review")
    assert delivered == [WEBHOOK_URL]


async def test_healthy_teams_finishes_while_webhook_stalls_but_lambda_waits(monkeypatch):
    _enable_channels("webhook", "teams")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-lambda")
    webhook_started = threading.Event()
    release_webhook = threading.Event()
    webhook_done = threading.Event()
    teams_started = threading.Event()
    teams_done = threading.Event()

    def fake_post(url, **kwargs):
        assert kwargs["timeout"] == 5 and kwargs["allow_redirects"] is False
        if url == WEBHOOK_URL:
            webhook_started.set()
            release_webhook.wait(3)
            webhook_done.set()
        else:
            teams_started.set()
            assert webhook_started.wait(2), "both network sinks must start before either finishes"
            teams_done.set()
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(output_sinks.requests, "post", fake_post)
    task = asyncio.create_task(push_outputs_async("review", payload={"a": 1}))
    try:
        assert await asyncio.to_thread(teams_started.wait, 2)
        assert await asyncio.to_thread(webhook_started.wait, 2)
        assert await asyncio.to_thread(teams_done.wait, 2)
        await asyncio.sleep(0.1)
        assert not task.done(), "Lambda must not return while an accepted delivery is running"
    finally:
        release_webhook.set()
    await asyncio.wait_for(task, 3)
    assert webhook_done.is_set()


async def test_a_stalled_channel_does_not_starve_healthy_outputs_across_commands(monkeypatch):
    _enable_channels("webhook", "teams")
    release_webhook = threading.Event()
    webhook_started = threading.Event()
    teams_posts = []

    def fake_post(url, **_kwargs):
        if url == WEBHOOK_URL:
            webhook_started.set()
            release_webhook.wait(3)
        else:
            assert webhook_started.wait(2), "Teams must not finish before the stalled webhook starts"
            teams_posts.append(url)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(output_sinks.requests, "post", fake_post)
    with ThreadPoolExecutor(max_workers=1) as webhook_pool:
        monkeypatch.setitem(run_output._NETWORK_EXECUTORS, "webhook", webhook_pool)
        tasks = [asyncio.create_task(push_outputs_async("review")) for _ in range(3)]
        try:
            assert await asyncio.to_thread(webhook_started.wait, 2)

            async def wait_for_teams():
                while len(teams_posts) < 3:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_teams(), 2)
            assert all(not task.done() for task in tasks)
        finally:
            release_webhook.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 3)
    assert teams_posts == [TEAMS_URL] * 3


async def test_slow_delivery_waits_leave_default_executor_available(monkeypatch):
    _enable_channels("webhook")
    release_webhook = threading.Event()
    both_started = threading.Event()
    started = 0
    lock = threading.Lock()
    teams_posted = threading.Event()

    def fake_post(url, **_kwargs):
        nonlocal started
        if url == WEBHOOK_URL:
            with lock:
                started += 1
                if started == 2:
                    both_started.set()
            release_webhook.wait(3)
        else:
            teams_posted.set()
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(output_sinks.requests, "post", fake_post)
    loop = asyncio.get_running_loop()
    previous_executor = loop._default_executor
    with ThreadPoolExecutor(max_workers=2) as small_pool:
        loop.set_default_executor(small_pool)
        tasks = [asyncio.create_task(push_outputs_async("review")) for _ in range(2)]
        try:
            async def wait_for_both_webhooks():
                while not both_started.is_set():
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_both_webhooks(), 2)
            _enable_channels("teams")
            await asyncio.wait_for(push_outputs_async("review"), 2)
            assert teams_posted.is_set()
            assert all(not task.done() for task in tasks)
        finally:
            release_webhook.set()
            try:
                await asyncio.wait_for(asyncio.gather(*tasks), 3)
            finally:
                loop._default_executor = previous_executor


async def test_cancelled_command_does_not_discard_queued_network_delivery(monkeypatch):
    _enable_channels("webhook")
    first_started = threading.Event()
    release_first = threading.Event()
    second_submitted = threading.Event()
    posted = []

    class SingleWorkerPool(ThreadPoolExecutor):
        submissions = 0

        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            self.submissions += 1
            if self.submissions == 2:
                second_submitted.set()
            return future

    def fake_post(url, **_kwargs):
        posted.append(url)
        if len(posted) == 1:
            first_started.set()
            release_first.wait(3)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(output_sinks.requests, "post", fake_post)
    with SingleWorkerPool(max_workers=1) as pool:
        monkeypatch.setitem(run_output._NETWORK_EXECUTORS, "webhook", pool)
        first = asyncio.create_task(push_outputs_async("review"))
        try:
            assert await asyncio.to_thread(first_started.wait, 2)
            second = asyncio.create_task(push_outputs_async("review"))
            assert await asyncio.to_thread(second_submitted.wait, 2)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(second, 2)
        finally:
            release_first.set()
            await asyncio.wait_for(first, 3)
    assert posted == [WEBHOOK_URL, WEBHOOK_URL]


async def test_cancelled_submission_queued_in_default_executor_still_finishes(monkeypatch):
    _enable_channels("webhook")
    delivered = threading.Event()
    release_worker = threading.Event()
    worker_started = threading.Event()

    def fake_post(_url, **_kwargs):
        delivered.set()
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(output_sinks.requests, "post", fake_post)

    def block_default_worker():
        worker_started.set()
        release_worker.wait(3)

    loop = asyncio.get_running_loop()
    previous_executor = loop._default_executor
    with ThreadPoolExecutor(max_workers=1) as small_pool:
        loop.set_default_executor(small_pool)
        blocker = loop.run_in_executor(None, block_default_worker)
        try:
            while not worker_started.is_set():
                await asyncio.sleep(0)
            task = asyncio.create_task(push_outputs_async("review"))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        finally:
            release_worker.set()
            await asyncio.wait_for(blocker, 3)
            assert await asyncio.wait_for(asyncio.to_thread(delivered.wait, 2), 3)
            loop._default_executor = previous_executor


async def test_asyncio_run_shutdown_does_not_drop_cancelled_command_output(monkeypatch):
    _enable_channels("file", "webhook")
    file_started = threading.Event()
    release_file = threading.Event()
    webhook_delivered = threading.Event()

    def slow_file(_self, _record, _cfg):
        file_started.set()
        release_file.wait(3)

    def fake_post(_url, **_kwargs):
        webhook_delivered.set()
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(output_sinks.FileSink, "send", slow_file)
    monkeypatch.setattr(output_sinks.requests, "post", fake_post)

    def run_short_lived_command():
        async def main():
            task = asyncio.create_task(push_outputs_async("review"))
            assert await asyncio.to_thread(file_started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            threading.Timer(0.1, release_file.set).start()

        asyncio.run(main())

    try:
        await asyncio.wait_for(asyncio.to_thread(run_short_lived_command), 4)
    finally:
        release_file.set()
    assert webhook_delivered.is_set(), "runner teardown must not drop output after cancellation"


async def test_failed_executor_submission_does_not_double_send_or_return_early(monkeypatch):
    _enable_channels("webhook")
    posted = []

    class RaiseAfterEnqueue(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            super().submit(*args, **kwargs)
            raise RuntimeError("failed after enqueueing")

    def fake_post(url, **_kwargs):
        posted.append(url)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(output_sinks.requests, "post", fake_post)
    with RaiseAfterEnqueue(max_workers=1) as pool:
        monkeypatch.setitem(run_output._NETWORK_EXECUTORS, "webhook", pool)
        await push_outputs_async("review")
        assert posted == [WEBHOOK_URL], "fallback must finish before the command returns"
    assert posted == [WEBHOOK_URL]


async def test_custom_sinks_keep_order_mutability_and_request_context(monkeypatch):
    seen = []
    request_context = ContextVar("output_custom_context")

    class FirstSink:
        def send(self, record, _cfg):
            seen.append(("first", request_context.get()))
            record["payload"]["value"] = 2
            request_context.set("updated")

    class SecondSink:
        def send(self, record, _cfg):
            seen.append(("second", record["payload"]["value"], request_context.get()))

    monkeypatch.setitem(output_sinks.OUTPUT_SINK_TYPES, "first_custom", FirstSink)
    monkeypatch.setitem(output_sinks.OUTPUT_SINK_TYPES, "second_custom", SecondSink)
    _enable_channels("first_custom", "second_custom")
    token = request_context.set("inherited")
    try:
        await push_outputs_async("review", payload={"value": 1})
    finally:
        request_context.reset(token)
    assert seen == [("first", "inherited"), ("second", 2, "updated")]


async def test_overridden_builtin_sink_keeps_registry_order(monkeypatch):
    seen = []

    class CustomWebhook:
        def send(self, record, _cfg):
            record["payload"]["value"] = 2
            seen.append("custom webhook")

    def fake_post(url, **kwargs):
        assert url == TEAMS_URL
        assert '"value": 2' in kwargs["json"]["attachments"][0]["content"]["body"][1]["text"]
        seen.append("teams")
        return SimpleNamespace(status_code=200)

    monkeypatch.setitem(output_sinks.OUTPUT_SINK_TYPES, "webhook", CustomWebhook)
    monkeypatch.setattr(output_sinks.requests, "post", fake_post)
    _enable_channels("webhook", "teams")

    await push_outputs_async("review", payload={"value": 1})

    assert seen == ["custom webhook", "teams"]


async def test_async_http_failure_keeps_credentials_out_of_logs(monkeypatch):
    _enable_channels("webhook")
    warnings = []

    def fail(url, **_kwargs):
        raise RuntimeError("secret URL: " + url)

    monkeypatch.setattr(output_sinks.requests, "post", fail)
    monkeypatch.setattr(run_output, "get_logger", lambda: SimpleNamespace(warning=warnings.append))
    await push_outputs_async("review")
    assert warnings == ["push_outputs: webhook failed: RuntimeError"]


def test_the_commands_do_not_import_the_blocking_delivery():
    for module in (pr_description, pr_reviewer):
        assert "push_outputs" not in vars(module), f"{module.__name__} imports the blocking delivery"
