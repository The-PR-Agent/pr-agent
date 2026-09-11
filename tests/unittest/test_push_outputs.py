import json
from types import SimpleNamespace

import pytest

from pr_agent.algo import utils
from pr_agent.algo.utils import get_settings, push_outputs


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

        monkeypatch.setattr(utils.requests, 'post', fake_post)

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

        monkeypatch.setattr(utils.requests, 'post', boom)

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
        monkeypatch.setattr(utils.requests, 'post',
                            lambda url, **kwargs: posts.append(url) or SimpleNamespace(status_code=200))

        push_outputs("review", payload={"a": 1}, markdown="hi")

        assert posts == [], f"{bad_url} should not have been POSTed to"

    def test_https_sink_url_is_accepted(self, monkeypatch):
        get_settings().set('PUSH_OUTPUTS.ENABLE', True)
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['webhook'])
        get_settings().set('PUSH_OUTPUTS.WEBHOOK_URL', 'https://example.test/hook')

        posts = []
        monkeypatch.setattr(utils.requests, 'post',
                            lambda url, **kwargs: posts.append(url) or SimpleNamespace(status_code=200))

        push_outputs("review", payload={"a": 1}, markdown="hi")

        assert posts == ['https://example.test/hook']

    def test_setup_errors_remain_non_fatal_and_secret_safe(self, monkeypatch):
        warnings = []

        def fail_settings():
            raise RuntimeError("secret setup marker")

        monkeypatch.setattr(utils, 'get_settings', fail_settings)
        monkeypatch.setattr(utils, 'get_logger',
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

        monkeypatch.setattr(utils.requests, 'post', fake_post)
        monkeypatch.setattr(utils, 'get_logger',
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

        monkeypatch.setattr(utils.requests, 'post', fake_post)
        monkeypatch.setattr(utils, 'get_logger',
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
        monkeypatch.setattr(utils.requests, 'post',
                            lambda *args, **kwargs: SimpleNamespace(status_code=status_code))
        monkeypatch.setattr(utils, 'get_logger',
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
        monkeypatch.setattr(utils.requests, 'post',
                            lambda url, **kwargs: posts.append(url) or SimpleNamespace(status_code=200))
        monkeypatch.setattr(utils, 'get_logger',
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
        monkeypatch.setattr(utils.requests, 'post',
                            lambda url, **kwargs: posts.append(url) or SimpleNamespace(status_code=200))
        monkeypatch.setattr(utils, 'get_logger',
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
        monkeypatch.setattr(utils.requests, 'post',
                            lambda url, **kwargs: posts.append(url) or SimpleNamespace(status_code=200))
        monkeypatch.setattr(utils, 'get_logger',
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

        monkeypatch.setattr(utils.requests, 'post', fake_post)
        monkeypatch.setattr(utils, 'get_logger',
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
        monkeypatch.setattr(utils.requests, 'post',
                            lambda *args, **kwargs: SimpleNamespace(status_code=429, text="response-secret"))
        monkeypatch.setattr(utils, 'get_logger',
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
        monkeypatch.setattr(utils.requests, 'post',
                            lambda url, **kwargs: posts.append(url) or SimpleNamespace(status_code=200))

        push_outputs("review", payload={"a": 1}, markdown="hi")

        assert posts == [webhook_url]
