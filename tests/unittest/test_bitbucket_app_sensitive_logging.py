import json

import pytest
from starlette.background import BackgroundTasks

from pr_agent.servers import bitbucket_app


class _Request:
    def __init__(self, headers, payload):
        self.headers = headers
        self._payload = payload
        self.json_calls = 0

    async def json(self):
        self.json_calls += 1
        return self._payload


class _RecordingLogger:
    def __init__(self):
        self.calls = []

    def __getattr__(self, level):
        def record(*args, **kwargs):
            self.calls.append((level, args, kwargs))

        return record


def _route_endpoint(path, method):
    return next(
        route.endpoint for route in bitbucket_app.router.routes if route.path == path and method in route.methods
    )


async def test_webhook_does_not_log_authorization_header(monkeypatch):
    token = "webhook-authorization-sentinel"
    authorization = f"jWt {token}"
    logger = _RecordingLogger()
    background_tasks = BackgroundTasks()
    monkeypatch.setattr(bitbucket_app, "get_logger", lambda: logger)

    result = await _route_endpoint("/webhook", "POST")(
        background_tasks,
        _Request({"authorization": authorization}, {"event": "pullrequest:created", "data": {}}),
    )

    assert result == "OK"
    assert len(background_tasks.tasks) == 1
    assert token not in repr(logger.calls)


@pytest.mark.parametrize("headers", [{}, {"authorization": "JWT"}, {"authorization": "Bearer token"}])
async def test_webhook_rejects_malformed_authorization_header(monkeypatch, headers):
    logger = _RecordingLogger()
    background_tasks = BackgroundTasks()
    request = _Request(headers, {"event": "pullrequest:created", "data": {}})
    monkeypatch.setattr(bitbucket_app, "get_logger", lambda: logger)

    result = await _route_endpoint("/webhook", "POST")(
        background_tasks,
        request,
    )

    assert result == "OK"
    assert request.json_calls == 0
    assert not background_tasks.tasks
    assert "Bitbucket webhook authorization header is malformed" in repr(logger.calls)


async def test_installed_webhook_does_not_log_credentials(monkeypatch):
    authorization = "JWT install-authorization-sentinel"
    shared_secret = "shared-secret-sentinel"
    logger = _RecordingLogger()
    stored = []
    secret_provider = type("SecretProvider", (), {"store_secret": lambda self, *args: stored.append(args)})()
    monkeypatch.setattr(bitbucket_app, "get_logger", lambda: logger)
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: secret_provider)

    result = await _route_endpoint("/installed", "POST")(
        _Request(
            {"authorization": authorization},
            {"sharedSecret": shared_secret, "clientKey": "client-key", "principal": {"username": "user"}},
        ),
        None,
    )

    logged = repr(logger.calls)
    assert result is None
    assert authorization not in logged
    assert shared_secret not in logged
    assert "handle_installed_webhooks" in logged
    assert json.loads(stored[0][1])["shared_secret"] == shared_secret


class _FailingJsonRequest(_Request):
    async def json(self):
        self.json_calls += 1
        raise ValueError("json-error-detail-sentinel")


async def test_installed_webhook_does_not_log_json_error_details(monkeypatch):
    logger = _RecordingLogger()
    monkeypatch.setattr(bitbucket_app, "get_logger", lambda: logger)

    result = await _route_endpoint("/installed", "POST")(
        _FailingJsonRequest({}, None),
        None,
    )

    logged = repr(logger.calls)
    assert result.status_code == 500
    assert "json-error-detail-sentinel" not in logged
    assert "Failed to register user: invalid JSON payload (ValueError)" in logged


@pytest.mark.parametrize(
    ("payload", "expected_field"),
    [
        (
            {"clientKey": "client-key-sentinel", "principal": {"username": "username-sentinel"}},
            "sharedSecret",
        ),
        (
            {"sharedSecret": "shared-secret-sentinel", "principal": {"username": "username-sentinel"}},
            "clientKey",
        ),
        (
            {"sharedSecret": "shared-secret-sentinel", "clientKey": "client-key-sentinel"},
            "principal",
        ),
        (
            {
                "sharedSecret": "shared-secret-sentinel",
                "clientKey": "client-key-sentinel",
                "principal": {},
            },
            "principal.username",
        ),
    ],
)
async def test_installed_webhook_logs_missing_field_without_values(monkeypatch, payload, expected_field):
    logger = _RecordingLogger()
    monkeypatch.setattr(bitbucket_app, "get_logger", lambda: logger)

    result = await _route_endpoint("/installed", "POST")(
        _Request({}, payload),
        None,
    )

    logged = repr(logger.calls)
    assert result.status_code == 500
    assert expected_field in logged
    assert "shared-secret-sentinel" not in logged
    assert "client-key-sentinel" not in logged
    assert "username-sentinel" not in logged


async def test_installed_webhook_rejects_non_object_payload_without_logging_values(monkeypatch):
    logger = _RecordingLogger()
    monkeypatch.setattr(bitbucket_app, "get_logger", lambda: logger)

    result = await _route_endpoint("/installed", "POST")(
        _Request({}, ["payload-value-sentinel"]),
        None,
    )

    logged = repr(logger.calls)
    assert result.status_code == 500
    assert "payload-value-sentinel" not in logged
    assert "installation payload must be a JSON object" in logged


async def test_installed_webhook_does_not_log_store_secret_error(monkeypatch):
    provider_error = "secret-provider-error-sentinel"
    logger = _RecordingLogger()

    class FailingSecretProvider:
        def store_secret(self, secret_name, secret_value):
            raise RuntimeError(provider_error)

    monkeypatch.setattr(bitbucket_app, "get_logger", lambda: logger)
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", FailingSecretProvider)

    result = await _route_endpoint("/installed", "POST")(
        _Request(
            {},
            {
                "sharedSecret": "shared-secret",
                "clientKey": "client-key",
                "principal": {"username": "user"},
            },
        ),
        None,
    )

    logged = repr(logger.calls)
    assert result.status_code == 500
    assert provider_error not in logged
    assert "Failed to register user: secret provider failure (RuntimeError)" in logged
