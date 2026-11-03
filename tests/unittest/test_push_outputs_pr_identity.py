import json
from types import SimpleNamespace

import pytest

from pr_agent.algo import output_sinks
from pr_agent.algo.run_output import push_outputs
from pr_agent.config_loader import get_settings

PR_IDENTITY = {
    "url": "https://github.com/org/repo/pull/123",
    "provider": "github",
    "repo": "org/repo",
    "number": 123,
    "head_sha": "9fceb02",
}


class FakeProvider:
    def get_pr_identity(self):
        return dict(PR_IDENTITY)


@pytest.fixture(autouse=True)
def _reset_push_outputs():
    yield
    s = get_settings()
    s.set('PUSH_OUTPUTS.ENABLE', False)
    s.set('PUSH_OUTPUTS.CHANNELS', [])
    s.set('PUSH_OUTPUTS.FILE_PATH', 'pr-agent-outputs/reviews.jsonl')
    s.set('PUSH_OUTPUTS.SLACK_WEBHOOK_URL', '')
    s.set('PUSH_OUTPUTS.TELEGRAM_BOT_TOKEN', '')
    s.set('PUSH_OUTPUTS.TELEGRAM_CHAT_ID', '')


def test_record_carries_the_calling_providers_identity(tmp_path):
    out = tmp_path / 'out.jsonl'
    get_settings().set('PUSH_OUTPUTS.ENABLE', True)
    get_settings().set('PUSH_OUTPUTS.CHANNELS', ['file'])
    get_settings().set('PUSH_OUTPUTS.FILE_PATH', str(out))

    push_outputs("review", payload={"a": 1}, markdown="hi", git_provider=FakeProvider())

    record = json.loads(out.read_text(encoding='utf-8'))
    assert record["pr"] == PR_IDENTITY


def test_review_identity_can_be_passed_directly(tmp_path):
    out = tmp_path / 'out.jsonl'
    get_settings().set('PUSH_OUTPUTS.ENABLE', True)
    get_settings().set('PUSH_OUTPUTS.CHANNELS', ['file'])
    get_settings().set('PUSH_OUTPUTS.FILE_PATH', str(out))

    push_outputs("review", payload={"a": 1}, markdown="hi", pr_identity=PR_IDENTITY)

    record = json.loads(out.read_text(encoding='utf-8'))
    assert record["pr"] == PR_IDENTITY


def test_record_without_a_provider_keeps_its_previous_shape(tmp_path):
    out = tmp_path / 'out.jsonl'
    get_settings().set('PUSH_OUTPUTS.ENABLE', True)
    get_settings().set('PUSH_OUTPUTS.CHANNELS', ['file'])
    get_settings().set('PUSH_OUTPUTS.FILE_PATH', str(out))

    push_outputs("review", payload={"a": 1}, markdown="hi")

    record = json.loads(out.read_text(encoding='utf-8'))
    assert "pr" not in record


def test_slack_appends_the_pull_request_url(monkeypatch):
    get_settings().set('PUSH_OUTPUTS.ENABLE', True)
    get_settings().set('PUSH_OUTPUTS.CHANNELS', ['slack'])
    get_settings().set('PUSH_OUTPUTS.SLACK_WEBHOOK_URL', 'https://example.test/slack')
    posts = []
    monkeypatch.setattr(output_sinks.requests, 'post',
                        lambda url, **kwargs: posts.append(kwargs['json']) or SimpleNamespace(status_code=200))

    push_outputs("review", payload={}, markdown="a review", git_provider=FakeProvider())

    assert posts == [{"text": f"a review\n\n{PR_IDENTITY['url']}"}]


def test_telegram_appends_the_pull_request_url(monkeypatch):
    get_settings().set('PUSH_OUTPUTS.ENABLE', True)
    get_settings().set('PUSH_OUTPUTS.CHANNELS', ['telegram'])
    get_settings().set('PUSH_OUTPUTS.TELEGRAM_BOT_TOKEN', 'token')
    get_settings().set('PUSH_OUTPUTS.TELEGRAM_CHAT_ID', 'chat')
    posts = []
    monkeypatch.setattr(output_sinks.requests, 'post',
                        lambda url, **kwargs: posts.append(kwargs['json']) or SimpleNamespace(status_code=200))

    push_outputs("review", payload={}, markdown="a review", git_provider=FakeProvider())

    assert posts == [{"chat_id": "chat", "text": f"a review\n\n{PR_IDENTITY['url']}"}]


def test_telegram_keeps_the_url_when_truncating_long_output(monkeypatch):
    get_settings().set('PUSH_OUTPUTS.ENABLE', True)
    get_settings().set('PUSH_OUTPUTS.CHANNELS', ['telegram'])
    get_settings().set('PUSH_OUTPUTS.TELEGRAM_BOT_TOKEN', 'token')
    get_settings().set('PUSH_OUTPUTS.TELEGRAM_CHAT_ID', 'chat')
    posts = []
    monkeypatch.setattr(output_sinks.requests, 'post',
                        lambda url, **kwargs: posts.append(kwargs['json']) or SimpleNamespace(status_code=200))

    push_outputs("review", payload={}, markdown="x" * 5000, git_provider=FakeProvider())

    text = posts[0]["text"]
    assert text.endswith(PR_IDENTITY['url'])
    assert len(text.encode('utf-16-le')) <= 8192
