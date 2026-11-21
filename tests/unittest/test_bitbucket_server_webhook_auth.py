import copy
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.testclient import TestClient
from starlette_context.middleware import RawContextMiddleware

from pr_agent.config_loader import global_settings
from pr_agent.servers import bitbucket_server_webhook as webhook

SECRET = "test-webhook-secret"
PAYLOAD = {
    "eventKey": "pr:comment:added",
    "comment": {"text": "/review"},
    "pullRequest": {
        "id": 7,
        "toRef": {"repository": {"slug": "repo", "project": {"key": "PROJ"}}},
    },
}
BODY = json.dumps(PAYLOAD).encode()


@pytest.fixture
def secured_client(monkeypatch):
    settings = copy.deepcopy(global_settings)
    settings.set("BITBUCKET_SERVER.WEBHOOK_SECRET", SECRET)
    settings.set("BITBUCKET_SERVER.URL", "https://bitbucket.example.test")
    monkeypatch.setattr(webhook, "get_settings", lambda: settings)
    dispatch = AsyncMock()
    logger = Mock()
    monkeypatch.setattr(webhook, "_run_commands_sequentially", dispatch)
    monkeypatch.setattr(webhook, "get_logger", lambda: logger)
    app = FastAPI(middleware=[Middleware(RawContextMiddleware)])
    app.include_router(webhook.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, settings, dispatch, logger


def _signature(body):
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.parametrize("secret", [None, ""])
@pytest.mark.parametrize("body", [BODY, b"not json", b'{"test": true}'])
def test_unconfigured_secret_rejects_before_processing(secured_client, secret, body):
    client, settings, dispatch, logger = secured_client
    settings.set("BITBUCKET_SERVER.WEBHOOK_SECRET", secret)

    for headers in ({}, {"x-hub-signature": _signature(body)}):
        response = client.post("/webhook", content=body, headers=headers)
        assert response.status_code == 403
        assert response.json() == {"detail": "Webhook authentication is not configured."}
    dispatch.assert_not_awaited()
    assert logger.error.call_count == 2
    for call in logger.error.call_args_list:
        assert "BITBUCKET_SERVER.WEBHOOK_SECRET" in call.args[0]
    logger.info.assert_not_called()


@pytest.mark.parametrize("headers", [{}, {"x-hub-signature": "sha256=invalid"}])
def test_missing_or_wrong_signature_rejects_without_dispatch(secured_client, headers):
    client, _settings, dispatch, _logger = secured_client

    response = client.post("/webhook", content=BODY, headers=headers)

    assert response.status_code == 403
    dispatch.assert_not_awaited()


def test_tampered_payload_rejects_without_dispatch(secured_client):
    client, _settings, dispatch, _logger = secured_client

    response = client.post("/webhook", content=BODY + b" ", headers={"x-hub-signature": _signature(BODY)})

    assert response.status_code == 403
    dispatch.assert_not_awaited()


@pytest.mark.parametrize("path", ["/webhook", "/"])
def test_valid_signature_dispatches_comment_command(secured_client, path):
    client, _settings, dispatch, _logger = secured_client

    response = client.post(path, content=BODY, headers={"x-hub-signature": _signature(BODY)})

    assert response.status_code == 200
    assert response.json() == {"message": "success"}
    dispatch.assert_awaited_once()
    assert dispatch.await_args.args[:2] == (
        ["/review"], "https://bitbucket.example.test/projects/PROJ/repos/repo/pull-requests/7",
    )


def test_configured_connection_test_does_not_dispatch(secured_client):
    client, _settings, dispatch, _logger = secured_client

    response = client.post("/webhook", content=b'{"test": true}')

    assert response.status_code == 200
    assert response.json() == {"message": "connection test successful"}
    dispatch.assert_not_awaited()


def _post_real_comment_webhook(monkeypatch, comment_text, *, configure_settings=None, enforce_policy=None):
    """Deliver a signed pr:comment:added webhook through the real sequential dispatcher."""
    import pr_agent.agent.pr_agent as pr_agent_module

    settings = copy.deepcopy(global_settings)
    settings.set("BITBUCKET_SERVER.WEBHOOK_SECRET", SECRET)
    settings.set("BITBUCKET_SERVER.URL", "https://bitbucket.example.test")
    if configure_settings is not None:
        configure_settings(settings)
    monkeypatch.setattr(webhook, "global_settings", settings)
    monkeypatch.setattr(webhook, "get_settings", lambda: settings)
    monkeypatch.setattr(webhook, "apply_repo_settings", lambda _url: None)
    agent_repo_settings = Mock(return_value=None)
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", agent_repo_settings)
    if enforce_policy is not None:
        monkeypatch.setattr(pr_agent_module, "enforce_request_policy", enforce_policy)
    else:
        monkeypatch.setattr(pr_agent_module, "enforce_request_policy", lambda _url: True)
    prepare_spy = Mock(side_effect=AssertionError("comment commands must not go through prepare_command"))
    monkeypatch.setattr(webhook, "prepare_command", prepare_spy)

    ran = []

    class RecordingCommand:
        def __init__(self, pr_url, args=None, **_kwargs):
            ran.append((pr_url, args))

        async def run(self):
            return ""

    monkeypatch.setitem(pr_agent_module.command2class, "review", RecordingCommand)
    monkeypatch.setitem(pr_agent_module.command2class, "ask", RecordingCommand)

    payload = {
        **PAYLOAD,
        "comment": {"text": comment_text},
    }
    body = json.dumps(payload).encode()
    app = FastAPI(middleware=[Middleware(RawContextMiddleware)])
    app.include_router(webhook.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/webhook", content=body, headers={"x-hub-signature": _signature(body)})
    return response, ran, prepare_spy, agent_repo_settings


def test_comment_with_forbidden_arg_is_rejected_by_agent_without_logging_secret(monkeypatch):
    from pr_agent.log import get_logger

    records = []
    sink_id = get_logger().add(records.append, level="INFO")
    try:
        response, ran, prepare_spy, agent_repo_settings = _post_real_comment_webhook(
            monkeypatch, "/review --openai.key=top-secret-token"
        )
    finally:
        get_logger().remove(sink_id)

    assert response.status_code == 200
    assert ran == []
    agent_repo_settings.assert_called_once()
    prepare_spy.assert_not_called()
    assert any("is forbidden" in str(record) for record in records)
    assert all(
        "top-secret-token" not in str(record) and "top-secret-token" not in str(record.record["extra"])
        for record in records
    )


def test_comment_cannot_override_policy_settings_before_enforcement(monkeypatch):
    from pr_agent.config_loader import get_settings

    seen_ignore_titles = []

    def check_policy(_url):
        seen_ignore_titles.append(list(get_settings().get("CONFIG.IGNORE_PR_TITLE", [])))
        return False

    response, ran, prepare_spy, agent_repo_settings = _post_real_comment_webhook(
        monkeypatch,
        "/review --config.ignore_pr_title=[]",
        configure_settings=lambda s: s.set("CONFIG.IGNORE_PR_TITLE", ["WIP"]),
        enforce_policy=check_policy,
    )

    assert response.status_code == 200
    assert ran == []
    assert seen_ignore_titles == [["WIP"]]
    agent_repo_settings.assert_called_once()
    prepare_spy.assert_not_called()


def test_comment_ask_with_unmatched_single_quote_reaches_command(monkeypatch):
    response, ran, prepare_spy, agent_repo_settings = _post_real_comment_webhook(
        monkeypatch, "/ask Why does 'foo fail?"
    )

    assert response.status_code == 200
    assert ran == [
        (
            "https://bitbucket.example.test/projects/PROJ/repos/repo/pull-requests/7",
            ["Why", "does", "'foo", "fail?"],
        )
    ]
    agent_repo_settings.assert_called_once()
    prepare_spy.assert_not_called()


def test_pr_opened_auto_commands_still_use_prepare_command(monkeypatch):
    settings = copy.deepcopy(global_settings)
    settings.set("BITBUCKET_SERVER.WEBHOOK_SECRET", SECRET)
    settings.set("BITBUCKET_SERVER.URL", "https://bitbucket.example.test")
    settings.set("BITBUCKET_SERVER.PR_COMMANDS", ["/describe --pr_description.final_update_message=false"])
    settings.set("CONFIG.DISABLE_AUTO_FEEDBACK", False)
    monkeypatch.setattr(webhook, "global_settings", settings)
    monkeypatch.setattr(webhook, "get_settings", lambda: settings)
    monkeypatch.setattr(webhook, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(webhook, "should_process_pr_logic", lambda _data: True)
    seen = []
    agent = Mock()
    agent.handle_request = AsyncMock(side_effect=lambda url, body: seen.append((url, body)) or True)
    monkeypatch.setattr(webhook, "PRAgent", lambda: agent)

    payload = {
        "eventKey": "pr:opened",
        "pullRequest": {
            "id": 7,
            "toRef": {"repository": {"slug": "repo", "project": {"key": "PROJ"}}},
        },
    }
    body = json.dumps(payload).encode()
    app = FastAPI(middleware=[Middleware(RawContextMiddleware)])
    app.include_router(webhook.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/webhook", content=body, headers={"x-hub-signature": _signature(body)})

    assert response.status_code == 200
    assert seen == [
        (
            "https://bitbucket.example.test/projects/PROJ/repos/repo/pull-requests/7",
            ["/describe", "--pr_description.final_update_message=false"],
        )
    ]
