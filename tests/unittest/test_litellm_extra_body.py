import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

import pr_agent.algo.ai_handlers.litellm_helpers as helpers


def _configure(monkeypatch, value):
    monkeypatch.setattr(
        helpers, "get_settings", lambda: SimpleNamespace(litellm=SimpleNamespace(extra_body=value))
    )


@pytest.mark.parametrize("template", [{"enable_thinking": False}, {}])
def test_chat_template_kwargs_are_nested_without_changing_existing_body(monkeypatch, template):
    _configure(monkeypatch, json.dumps({"chat_template_kwargs": template, "service_tier": "flex"}))
    existing = {"provider": {"only": ["provider-a"]}, "reasoning": {"effort": "low"}}
    original = deepcopy(existing)
    kwargs = {"model": "openai/qwen", "extra_body": existing}

    result = helpers._process_litellm_extra_body(kwargs)

    assert result is kwargs
    assert result["extra_body"] == {**original, "chat_template_kwargs": template}
    assert result["service_tier"] == "flex"
    assert "chat_template_kwargs" not in result
    assert existing == original


def test_chat_template_kwargs_create_extra_body(monkeypatch):
    _configure(monkeypatch, '{"chat_template_kwargs": {"enable_thinking": false}}')
    assert helpers._process_litellm_extra_body({}) == {
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}
    }


@pytest.mark.parametrize(
    "configuration, kwargs, message",
    [
        ('{"chat_template_kwargs": false}', {}, "must be a JSON object"),
        ('{"chat_template_kwargs": []}', {}, "must be a JSON object"),
        ('{"chat_template_kwargs": null}', {}, "must be a JSON object"),
        ('{"chat_template_kwargs": {}}', {"extra_body": "invalid"}, "must be a mapping"),
        ('{"chat_template_kwargs": {}}', {"extra_body": {"chat_template_kwargs": {}}}, "cannot override"),
        ('{"chat_template_kwargs": {}}', {"chat_template_kwargs": {}}, "cannot override"),
        ('{"service_tier": "flex"}', {"service_tier": "auto"}, "cannot override"),
        ('{"unknown": true}', {}, "unsupported keys"),
        ('[]', {}, "must be a JSON object"),
        ('{', {}, "invalid JSON"),
    ],
)
def test_invalid_extra_body_does_not_mutate_kwargs(monkeypatch, configuration, kwargs, message):
    _configure(monkeypatch, configuration)
    original = deepcopy(kwargs)
    with pytest.raises(ValueError, match=message):
        helpers._process_litellm_extra_body(kwargs)
    assert kwargs == original


def test_existing_controls_remain_top_level(monkeypatch):
    _configure(monkeypatch, '{"processing_mode": "flex", "service_tier": "flex"}')
    assert helpers._process_litellm_extra_body({}) == {
        "processing_mode": "flex", "service_tier": "flex"
    }
