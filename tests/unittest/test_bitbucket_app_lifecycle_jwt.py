import base64
import json
import time

import jwt
import pytest
from starlette.background import BackgroundTasks

from pr_agent.servers import bitbucket_app


class _Request:
    def __init__(self, headers, payload, method="POST", path="/installed", query=""):
        self.headers = headers
        self._payload = payload
        self.method = method
        self.url = type("URL", (), {"path": path, "query": query})()
        self.json_calls = 0

    async def json(self):
        self.json_calls += 1
        return self._payload


class _InMemorySecretProvider:
    def __init__(self, initial=None):
        self.secrets = dict(initial or {})
        self.stored = []

    def get_secret(self, key):
        return self.secrets.get(key, "")

    def store_secret(self, key, value):
        self.secrets[key] = value
        self.stored.append((key, value))


def _route_endpoint(path, method):
    return next(
        route.endpoint for route in bitbucket_app.router.routes if route.path == path and method in route.methods
    )


# --- /installed: Name shape validation and clientKey storage ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_key",
    [
        "client-key-123",
        "{b4578b87-bb47-4f65-8b3e-11a5efd2c94d}",
        "ari:cloud:bitbucket::workspace/12345",
        "my_workspace.atlassian.net",
    ],
)
async def test_installed_stores_secret_keyed_by_valid_client_key(monkeypatch, client_key):
    provider = _InMemorySecretProvider()
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: provider)

    payload = {
        "sharedSecret": "secret-val-123",
        "clientKey": client_key,
        "principal": {"username": "principal-user"},
    }
    request = _Request({}, payload)

    result = await _route_endpoint("/installed", "POST")(request, None)
    assert result is None
    assert len(provider.stored) == 1
    stored_key, stored_val = provider.stored[0]
    assert stored_key == bitbucket_app._bitbucket_client_secret_name(client_key)
    data = json.loads(stored_val)
    assert data["shared_secret"] == "secret-val-123"
    assert data["client_key"] == client_key
    assert data["username"] == "principal-user"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_client_key",
    [
        "",
        "../etc/passwd",
        "client/../traversal",
        "client key with spaces",
        "client\nkey",
        "a" * 257,
        None,
    ],
)
async def test_installed_rejects_invalid_client_key_shape(monkeypatch, bad_client_key):
    provider = _InMemorySecretProvider()
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: provider)

    payload = {
        "sharedSecret": "secret-val-123",
        "clientKey": bad_client_key,
        "principal": {"username": "principal-user"},
    }
    request = _Request({}, payload)

    result = await _route_endpoint("/installed", "POST")(request, None)
    assert result.status_code == 500
    assert len(provider.stored) == 0


# --- /installed: Re-installation JWT verification ---


@pytest.mark.asyncio
async def test_installed_first_install_succeeds_without_auth_header(monkeypatch):
    provider = _InMemorySecretProvider()
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: provider)

    payload = {
        "sharedSecret": "new-secret",
        "clientKey": "workspace-uuid-1",
        "principal": {"username": "user1"},
    }
    request = _Request({}, payload)

    result = await _route_endpoint("/installed", "POST")(request, None)
    assert result is None
    assert provider.get_secret(bitbucket_app._bitbucket_client_secret_name("workspace-uuid-1")) != ""


@pytest.mark.asyncio
async def test_installed_reinstall_rejects_without_auth_header(monkeypatch):
    old_secrets = {"shared_secret": "old-secret-val", "client_key": "workspace-uuid-1"}
    provider = _InMemorySecretProvider({"workspace-uuid-1": json.dumps(old_secrets)})
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: provider)

    payload = {
        "sharedSecret": "new-secret-val",
        "clientKey": "workspace-uuid-1",
        "principal": {"username": "user1"},
    }
    request = _Request({}, payload)  # No authorization header

    result = await _route_endpoint("/installed", "POST")(request, None)
    assert result.status_code == 401
    assert json.loads(result.body.decode())["error"] == "Unauthorized re-installation"
    assert len(provider.stored) == 0


@pytest.mark.asyncio
async def test_installed_reinstall_rejects_with_wrong_jwt_signature(monkeypatch):
    old_secrets = {"shared_secret": "old-secret-val-32-bytes-long-key!", "client_key": "workspace-uuid-1"}
    provider = _InMemorySecretProvider({"workspace-uuid-1": json.dumps(old_secrets)})
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: provider)

    bad_jwt = jwt.encode(
        {"iss": "workspace-uuid-1", "exp": int(time.time()) + 300},
        "wrong-secret-val-32-bytes-long-key!",
        algorithm="HS256",
    )
    payload = {
        "sharedSecret": "new-secret-val",
        "clientKey": "workspace-uuid-1",
        "principal": {"username": "user1"},
    }
    request = _Request({"authorization": f"JWT {bad_jwt}"}, payload)

    result = await _route_endpoint("/installed", "POST")(request, None)
    assert result.status_code == 401
    assert json.loads(result.body.decode())["error"] == "Unauthorized re-installation"
    assert len(provider.stored) == 0


@pytest.mark.asyncio
async def test_installed_reinstall_accepts_valid_jwt_signed_with_stored_secret(monkeypatch):
    old_secrets = {"shared_secret": "old-secret-val-32-bytes-long-key!", "client_key": "workspace-uuid-1"}
    provider = _InMemorySecretProvider({"workspace-uuid-1": json.dumps(old_secrets)})
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: provider)

    valid_jwt = jwt.encode(
        {"iss": "workspace-uuid-1", "exp": int(time.time()) + 300},
        "old-secret-val-32-bytes-long-key!",
        algorithm="HS256",
    )
    payload = {
        "sharedSecret": "new-secret-val",
        "clientKey": "workspace-uuid-1",
        "principal": {"username": "user1"},
    }
    request = _Request({"authorization": f"JWT {valid_jwt}"}, payload)

    result = await _route_endpoint("/installed", "POST")(request, None)
    assert result is None
    assert len(provider.stored) == 1
    assert json.loads(provider.stored[0][1])["shared_secret"] == "new-secret-val"


# --- /webhook: QSH and aud verification ---


@pytest.mark.asyncio
async def test_webhook_verifies_valid_qsh_without_aud_claim(monkeypatch):
    shared_secret = "secret-12345-very-long-secret-key-32bytes"
    client_key = "workspace-client-key"
    stored_secret = json.dumps({"shared_secret": shared_secret, "client_key": client_key})
    provider = _InMemorySecretProvider({client_key: stored_secret})
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: provider)

    # Compute expected QSH for POST /webhook
    expected_qsh = bitbucket_app._compute_qsh("POST", "/webhook")

    # Bitbucket Cloud JWT: contains iss, iat, exp, qsh, sub - NO aud claim!
    now = int(time.time())
    payload_jwt = {
        "iss": client_key,
        "iat": now,
        "exp": now + 300,
        "qsh": expected_qsh,
        "sub": "account-id-123",
    }
    token = jwt.encode(payload_jwt, shared_secret, algorithm="HS256")

    called_commands = []

    async def fake_perform_commands(*args, **kwargs):
        called_commands.append(args)

    async def fake_get_bearer_token(*args):
        return "bearer-token"

    monkeypatch.setattr(bitbucket_app, "_perform_commands_bitbucket", fake_perform_commands)
    monkeypatch.setattr(bitbucket_app, "get_bearer_token", fake_get_bearer_token)
    monkeypatch.setattr(bitbucket_app, "context", {})

    webhook_payload = {
        "event": "pullrequest:created",
        "data": {
            "actor": {"account_id": "account-id-123", "nickname": "testuser", "type": "user"},
            "pullrequest": {"links": {"html": {"href": "https://bitbucket.org/org/repo/pull-requests/1"}}},
        },
    }
    request = _Request(
        {"authorization": f"JWT {token}"},
        webhook_payload,
        method="POST",
        path="/webhook",
    )
    background_tasks = BackgroundTasks()

    result = await _route_endpoint("/webhook", "POST")(background_tasks, request)
    assert result == "OK"
    assert len(background_tasks.tasks) == 1

    # Run the background task
    await background_tasks()
    assert len(called_commands) == 1


@pytest.mark.asyncio
async def test_webhook_rejects_mismatched_qsh(monkeypatch):
    shared_secret = "secret-12345-very-long-secret-key-32bytes"
    client_key = "workspace-client-key"
    stored_secret = json.dumps({"shared_secret": shared_secret, "client_key": client_key})
    provider = _InMemorySecretProvider({client_key: stored_secret})
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: provider)

    now = int(time.time())
    payload_jwt = {
        "iss": client_key,
        "iat": now,
        "exp": now + 300,
        "qsh": "wrong-qsh-hash-value",
        "sub": "account-id-123",
    }
    token = jwt.encode(payload_jwt, shared_secret, algorithm="HS256")

    webhook_payload = {
        "event": "pullrequest:created",
        "data": {
            "actor": {"account_id": "account-id-123", "nickname": "testuser", "type": "user"},
            "pullrequest": {"links": {"html": {"href": "https://bitbucket.org/org/repo/pull-requests/1"}}},
        },
    }
    request = _Request(
        {"authorization": f"JWT {token}"},
        webhook_payload,
        method="POST",
        path="/webhook",
    )
    background_tasks = BackgroundTasks()

    result = await _route_endpoint("/webhook", "POST")(background_tasks, request)
    assert result == "OK"

    # Reject a mismatched qsh before the body is parsed or a task is queued.
    assert request.json_calls == 0
    assert not background_tasks.tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["malformed_token", "unknown_client_key", "bad_signature"])
async def test_webhook_rejects_invalid_jwt_before_parsing_body(monkeypatch, case):
    shared_secret = "secret-12345-very-long-secret-key-32bytes"
    client_key = "workspace-client-key"
    stored_secret = json.dumps({"shared_secret": shared_secret, "client_key": client_key})
    provider = _InMemorySecretProvider({client_key: stored_secret})
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: provider)

    now = int(time.time())
    claims = {
        "iss": client_key,
        "iat": now,
        "exp": now + 300,
        "qsh": bitbucket_app._compute_qsh("POST", "/webhook"),
    }
    if case == "malformed_token":
        token = "not-a-jwt"
    elif case == "unknown_client_key":
        token = jwt.encode({**claims, "iss": "unknown-client-key"}, shared_secret, algorithm="HS256")
    else:
        token = jwt.encode(claims, "wrong-secret-32-bytes-long-key!!", algorithm="HS256")

    webhook_payload = {
        "event": "pullrequest:created",
        "data": {
            "actor": {"account_id": "account-id-123", "nickname": "testuser", "type": "user"},
            "pullrequest": {"links": {"html": {"href": "https://bitbucket.org/org/repo/pull-requests/1"}}},
        },
    }
    request = _Request(
        {"authorization": f"JWT {token}"},
        webhook_payload,
        method="POST",
        path="/webhook",
    )
    background_tasks = BackgroundTasks()

    result = await _route_endpoint("/webhook", "POST")(background_tasks, request)
    assert result == "OK"

    # Reject an unverifiable JWT before the body is parsed or a task is queued.
    assert request.json_calls == 0
    assert not background_tasks.tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_secret", ["", 12345, None])
async def test_webhook_rejects_malformed_stored_secret_before_parsing_body(monkeypatch, stored_secret):
    shared_secret = "secret-12345-very-long-secret-key-32bytes"
    client_key = "workspace-client-key"
    stored = json.dumps({"shared_secret": stored_secret, "client_key": client_key})
    provider = _InMemorySecretProvider({client_key: stored})
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: provider)

    now = int(time.time())
    token = jwt.encode(
        {
            "iss": client_key,
            "iat": now,
            "exp": now + 300,
            "qsh": bitbucket_app._compute_qsh("POST", "/webhook"),
        },
        shared_secret,
        algorithm="HS256",
    )

    webhook_payload = {
        "event": "pullrequest:created",
        "data": {
            "actor": {"account_id": "account-id-123", "nickname": "testuser", "type": "user"},
            "pullrequest": {"links": {"html": {"href": "https://bitbucket.org/org/repo/pull-requests/1"}}},
        },
    }
    request = _Request(
        {"authorization": f"JWT {token}"},
        webhook_payload,
        method="POST",
        path="/webhook",
    )
    background_tasks = BackgroundTasks()

    result = await _route_endpoint("/webhook", "POST")(background_tasks, request)
    assert result == "OK"

    # Reject a stored secret that cannot verify a token before the body is parsed.
    assert request.json_calls == 0
    assert not background_tasks.tasks


@pytest.mark.asyncio
async def test_webhook_rejects_deeply_nested_claims_before_parsing_body():
    nested_claims = "[" * 10000 + "]" * 10000
    payload_segment = base64.urlsafe_b64encode(nested_claims.encode()).rstrip(b"=").decode()
    token = f"header.{payload_segment}.signature"

    webhook_payload = {
        "event": "pullrequest:created",
        "data": {
            "actor": {"account_id": "account-id-123", "nickname": "testuser", "type": "user"},
            "pullrequest": {"links": {"html": {"href": "https://bitbucket.org/org/repo/pull-requests/1"}}},
        },
    }
    request = _Request(
        {"authorization": f"JWT {token}"},
        webhook_payload,
        method="POST",
        path="/webhook",
    )
    background_tasks = BackgroundTasks()

    result = await _route_endpoint("/webhook", "POST")(background_tasks, request)
    assert result == "OK"

    # Reject claims that cannot be decoded before the body is parsed.
    assert request.json_calls == 0
    assert not background_tasks.tasks


@pytest.mark.asyncio
async def test_webhook_rejects_non_ascii_qsh_before_parsing_body(monkeypatch):
    shared_secret = "secret-12345-very-long-secret-key-32bytes"
    client_key = "workspace-client-key"
    stored_secret = json.dumps({"shared_secret": shared_secret, "client_key": client_key})
    provider = _InMemorySecretProvider({client_key: stored_secret})
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: provider)

    now = int(time.time())
    token = jwt.encode(
        {
            "iss": client_key,
            "iat": now,
            "exp": now + 300,
            "qsh": "qsh-\u2713-value",
        },
        shared_secret,
        algorithm="HS256",
    )

    webhook_payload = {
        "event": "pullrequest:created",
        "data": {
            "actor": {"account_id": "account-id-123", "nickname": "testuser", "type": "user"},
            "pullrequest": {"links": {"html": {"href": "https://bitbucket.org/org/repo/pull-requests/1"}}},
        },
    }
    request = _Request(
        {"authorization": f"JWT {token}"},
        webhook_payload,
        method="POST",
        path="/webhook",
    )
    background_tasks = BackgroundTasks()

    result = await _route_endpoint("/webhook", "POST")(background_tasks, request)
    assert result == "OK"

    # Reject a non-ASCII qsh before the body is parsed.
    assert request.json_calls == 0
    assert not background_tasks.tasks


# --- /installed: Fail-closed verification on provider or secret corruption ---


@pytest.mark.asyncio
async def test_installed_fails_closed_when_secret_provider_raises(monkeypatch):
    class _FailingSecretProvider:
        def get_secret(self, key):
            raise RuntimeError("Database connection lost")

    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: _FailingSecretProvider())
    payload = {
        "sharedSecret": "secret-val-123",
        "clientKey": "workspace-uuid-1",
        "principal": {"username": "user1"},
    }
    request = _Request({}, payload)
    result = await _route_endpoint("/installed", "POST")(request, None)
    assert result.status_code == 500
    assert json.loads(result.body.decode())["error"] == "Unable to verify existing installation"


@pytest.mark.asyncio
async def test_installed_fails_closed_when_stored_secret_malformed_json(monkeypatch):
    provider = _InMemorySecretProvider({"workspace-uuid-1": "{not-valid-json"})
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: provider)
    payload = {
        "sharedSecret": "secret-val-123",
        "clientKey": "workspace-uuid-1",
        "principal": {"username": "user1"},
    }
    request = _Request({}, payload)
    result = await _route_endpoint("/installed", "POST")(request, None)
    assert result.status_code == 500
    assert json.loads(result.body.decode())["error"] == "Unable to verify existing installation"
    assert len(provider.stored) == 0


@pytest.mark.asyncio
async def test_installed_fails_closed_when_stored_secret_missing_shared_secret(monkeypatch):
    provider = _InMemorySecretProvider({"workspace-uuid-1": json.dumps({"client_key": "workspace-uuid-1"})})
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: provider)
    payload = {
        "sharedSecret": "secret-val-123",
        "clientKey": "workspace-uuid-1",
        "principal": {"username": "user1"},
    }
    request = _Request({}, payload)
    result = await _route_endpoint("/installed", "POST")(request, None)
    assert result.status_code == 500
    assert json.loads(result.body.decode())["error"] == "Unable to verify existing installation"
    assert len(provider.stored) == 0
