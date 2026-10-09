import json
from types import SimpleNamespace

import jwt
import pytest
from starlette.background import BackgroundTasks

from pr_agent.servers import bitbucket_app


class _Request:
    def __init__(self, headers, payload, method="POST", path="/webhook"):
        self.headers = headers
        self._payload = payload
        self.method = method
        self.url = type("URL", (), {"path": path, "query": ""})()
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
    sentinel = "webhook-authorization-sentinel"
    token = f"e30.eyJpc3MiOiJjbGllbnQifQ.{sentinel}"
    authorization = f"jWt {token}"
    logger = _RecordingLogger()
    background_tasks = BackgroundTasks()
    secret_provider = type(
        "SecretProvider",
        (),
        {"get_secret": lambda self, _key: json.dumps({"shared_secret": "shared-secret"})},
    )()
    monkeypatch.setattr(bitbucket_app, "get_logger", lambda: logger)
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: secret_provider)
    monkeypatch.setattr(
        bitbucket_app.jwt,
        "decode",
        lambda *args, **kwargs: {"qsh": bitbucket_app._compute_qsh("POST", "/webhook")},
    )

    result = await _route_endpoint("/webhook", "POST")(
        background_tasks,
        _Request({"authorization": authorization}, {"event": "pullrequest:created", "data": {}}),
    )

    assert result == "OK"
    assert len(background_tasks.tasks) == 1
    assert sentinel not in repr(logger.calls)
    assert authorization not in repr(logger.calls)


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
    client_key = "ari:cloud:bitbucket::workspace/{client-key}"
    logger = _RecordingLogger()
    stored = []
    secret_provider = type("SecretProvider", (), {"store_secret": lambda self, *args: stored.append(args)})()
    monkeypatch.setattr(bitbucket_app, "get_logger", lambda: logger)
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: secret_provider)

    result = await _route_endpoint("/installed", "POST")(
        _Request(
            {"authorization": authorization},
            {"sharedSecret": shared_secret, "clientKey": client_key, "principal": {"username": "user"}},
        ),
        None,
    )

    logged = repr(logger.calls)
    assert result is None
    assert authorization not in logged
    assert shared_secret not in logged
    assert "handle_installed_webhooks" in logged
    assert stored[0][0] == bitbucket_app._bitbucket_client_secret_name(client_key)
    assert json.loads(stored[0][1])["shared_secret"] == shared_secret
    assert json.loads(stored[0][1])["client_key"] == client_key
    assert json.loads(stored[0][1])["username"] == "user"


async def test_webhook_looks_up_secret_by_hashed_client_key(monkeypatch):
    client_key = "ari:cloud:bitbucket::workspace/{client-key}"
    shared_secret = "shared-secret-with-enough-bytes-for-hs256"
    token = jwt.encode({"iss": client_key, "aud": "https://app.example"}, shared_secret, algorithm="HS256")
    looked_up = []

    class SecretProvider:
        def get_secret(self, secret_name):
            looked_up.append(secret_name)
            return json.dumps({"shared_secret": shared_secret, "client_key": client_key})

    async def get_bearer_token(*args):
        return "bearer-token"

    settings = SimpleNamespace(bitbucket=SimpleNamespace(base_url="https://app.example"))
    settings.get = lambda key, default=None: default
    monkeypatch.setattr(bitbucket_app, "get_settings", lambda: settings)
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: SecretProvider())
    monkeypatch.setattr(bitbucket_app, "get_bearer_token", get_bearer_token)

    background_tasks = BackgroundTasks()
    result = await _route_endpoint("/webhook", "POST")(
        background_tasks,
        _Request(
            {"authorization": f"JWT {token}"},
            {"event": "repo:push", "data": {"actor": {"type": "user"}}},
        ),
    )

    assert result == "OK"
    await background_tasks()
    assert looked_up == [bitbucket_app._bitbucket_client_secret_name(client_key)]


async def test_webhook_falls_back_to_legacy_client_key_secret(monkeypatch):
    client_key = "legacy-client-key"
    shared_secret = "shared-secret-with-enough-bytes-for-hs256"
    token = jwt.encode({"iss": client_key, "aud": "https://app.example"}, shared_secret, algorithm="HS256")
    looked_up = []

    class SecretProvider:
        def get_secret(self, secret_name):
            looked_up.append(secret_name)
            if secret_name == client_key:
                return json.dumps({"shared_secret": shared_secret, "client_key": client_key})
            return ""

    async def get_bearer_token(*args):
        return "bearer-token"

    settings = SimpleNamespace(bitbucket=SimpleNamespace(base_url="https://app.example"))
    settings.get = lambda key, default=None: default
    monkeypatch.setattr(bitbucket_app, "get_settings", lambda: settings)
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: SecretProvider())
    monkeypatch.setattr(bitbucket_app, "get_bearer_token", get_bearer_token)

    background_tasks = BackgroundTasks()
    result = await _route_endpoint("/webhook", "POST")(
        background_tasks,
        _Request(
            {"authorization": f"JWT {token}"},
            {"event": "repo:push", "data": {"actor": {"type": "user"}}},
        ),
    )

    assert result == "OK"
    await background_tasks()
    assert looked_up == [bitbucket_app._bitbucket_client_secret_name(client_key), client_key]


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


async def test_webhook_logs_only_selected_payload_fields(monkeypatch):
    logger = _RecordingLogger()
    background_tasks = BackgroundTasks()
    payload = {
        "event": "pullrequest:created",
        "clientKey": "webhook-client-key",
        "data": {
            "sharedSecret": "webhook-secret-sentinel",
            "description": "private-webhook-description",
        },
    }
    secret_provider = type(
        "SecretProvider",
        (),
        {"get_secret": lambda self, _key: json.dumps({"shared_secret": "shared-secret"})},
    )()
    monkeypatch.setattr(bitbucket_app, "get_logger", lambda: logger)
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: secret_provider)
    monkeypatch.setattr(
        bitbucket_app.jwt,
        "decode",
        lambda *args, **kwargs: {"qsh": bitbucket_app._compute_qsh("POST", "/webhook")},
    )

    result = await _route_endpoint("/webhook", "POST")(
        background_tasks,
        _Request({"authorization": "JWT e30.eyJpc3MiOiJjbGllbnQifQ.signature"}, payload),
    )

    logged = repr(logger.calls)
    assert result == "OK"
    assert "pullrequest:created" in logged
    assert "webhook-client-key" in logged
    assert "event" in logged and "data" in logged
    assert "webhook-secret-sentinel" not in logged
    assert "private-webhook-description" not in logged


async def test_uninstalled_webhook_logs_only_selected_payload_fields(monkeypatch):
    logger = _RecordingLogger()
    payload = {
        "clientKey": "uninstalled-client-key",
        "principal": {"username": "private-username"},
        "sharedSecret": "uninstalled-secret-sentinel",
    }
    monkeypatch.setattr(bitbucket_app, "get_logger", lambda: logger)

    await _route_endpoint("/uninstalled", "POST")(
        _Request({}, payload),
        None,
    )

    logged = repr(logger.calls)
    assert "uninstalled-client-key" in logged
    assert "clientKey" in logged and "principal" in logged and "sharedSecret" in logged
    assert "private-username" not in logged
    assert "uninstalled-secret-sentinel" not in logged
