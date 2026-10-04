from types import SimpleNamespace

import pytest

import pr_agent.algo.ai_handlers.litellm_helpers as helpers


def _configure(monkeypatch, value):
    monkeypatch.setattr(
        helpers, "get_settings", lambda: SimpleNamespace(litellm=SimpleNamespace(extra_body=value))
    )


def test_chat_template_kwargs_is_allowed(monkeypatch):
    _configure(monkeypatch, '{"chat_template_kwargs": {"enable_thinking": false}}')
    kwargs = {"extra_body": {"provider": {"only": ["a"]}}}
    assert helpers._process_litellm_extra_body(kwargs) == {
        "extra_body": {"provider": {"only": ["a"]}},
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_other_keys_still_rejected(monkeypatch):
    _configure(monkeypatch, '{"chat_template_kwargs": {}, "api_base": "http://x"}')
    with pytest.raises(ValueError, match="unsupported keys"):
        helpers._process_litellm_extra_body({})
