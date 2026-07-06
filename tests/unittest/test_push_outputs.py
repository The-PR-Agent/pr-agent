import json

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

        monkeypatch.setattr(utils.requests, 'post', fake_post)

        push_outputs("review", payload={"a": 1}, markdown="a markdown review")

        assert captured['url'] == slack_url
        assert captured['json'] == {"text": "a markdown review"}

    def test_errors_are_non_fatal(self, monkeypatch):
        get_settings().set('PUSH_OUTPUTS.ENABLE', True)
        get_settings().set('PUSH_OUTPUTS.CHANNELS', ['webhook'])
        get_settings().set('PUSH_OUTPUTS.WEBHOOK_URL', 'http://example.invalid/hook')

        def boom(*args, **kwargs):
            raise ConnectionError("no network")

        monkeypatch.setattr(utils.requests, 'post', boom)

        # Must not raise.
        push_outputs("review", payload={"a": 1}, markdown="hi")
