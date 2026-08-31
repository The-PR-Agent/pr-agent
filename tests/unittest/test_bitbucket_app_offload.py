from types import SimpleNamespace

import pytest

from pr_agent.servers import bitbucket_app


def _host_settings(request_timeout=30):
    return SimpleNamespace(get=lambda *args: request_timeout)


@pytest.mark.parametrize("request_timeout", [None, "", False, True, 0, -1, 10**400, float("inf"), float("nan")])
def test_request_timeout_rejects_invalid_values(monkeypatch, request_timeout):
    monkeypatch.setattr(bitbucket_app, "global_settings", _host_settings(request_timeout))

    with pytest.raises(ValueError, match="must be a positive finite number"):
        bitbucket_app._get_request_timeout()


def test_request_timeout_accepts_positive_numeric_strings(monkeypatch):
    monkeypatch.setattr(bitbucket_app, "global_settings", _host_settings("60"))

    assert bitbucket_app._get_request_timeout() == 60


def test_request_timeout_default_is_wired_to_configuration_toml():
    """Pin the real key name and shipped default without mocking settings."""
    from pr_agent.config_loader import global_settings

    assert global_settings.get("bitbucket_app.request_timeout") == 30
    assert bitbucket_app._get_request_timeout() == 30.0


def test_request_timeout_ignores_request_scoped_settings(monkeypatch):
    monkeypatch.setattr(bitbucket_app, "global_settings", _host_settings(30))
    monkeypatch.setattr(bitbucket_app, "get_settings", lambda: _host_settings(900))

    assert bitbucket_app._get_request_timeout() == 30


@pytest.mark.asyncio
async def test_get_bearer_token_offloads_blocking_request(monkeypatch):
    response = SimpleNamespace(json=lambda: {"access_token": "access-token"})
    calls = []

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return response

    def fail_if_called(*args, **kwargs):
        pytest.fail("Blocking request was called directly")

    settings = SimpleNamespace(bitbucket=SimpleNamespace(app_key="app-key"))
    monkeypatch.setattr(bitbucket_app, "get_settings", lambda: settings)
    monkeypatch.setattr(
        bitbucket_app,
        "global_settings",
        _host_settings(),
    )
    monkeypatch.setattr(bitbucket_app.jwt, "encode", lambda *args, **kwargs: "jwt-token")
    monkeypatch.setattr(bitbucket_app.requests, "request", fail_if_called)
    monkeypatch.setattr(bitbucket_app.asyncio, "to_thread", fake_to_thread)

    token = await bitbucket_app.get_bearer_token("shared-secret", "client-key")

    assert token == "access-token"
    assert len(calls) == 1
    func, args, kwargs = calls[0]
    assert func is bitbucket_app.requests.request
    assert args == ("POST", "https://bitbucket.org/site/oauth2/access_token")
    assert kwargs["data"] == "grant_type=urn%3Abitbucket%3Aoauth2%3Ajwt"
    assert kwargs["headers"]["Authorization"] == "JWT jwt-token"
    assert kwargs["timeout"] == 30


@pytest.mark.asyncio
async def test_get_bearer_token_propagates_request_timeout(monkeypatch):
    calls = []

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        raise bitbucket_app.requests.exceptions.Timeout

    def fail_if_called(*args, **kwargs):
        pytest.fail("Blocking request was called directly")

    settings = SimpleNamespace(bitbucket=SimpleNamespace(app_key="app-key"))
    monkeypatch.setattr(bitbucket_app, "get_settings", lambda: settings)
    monkeypatch.setattr(bitbucket_app, "global_settings", _host_settings())
    monkeypatch.setattr(bitbucket_app.jwt, "encode", lambda *args, **kwargs: "jwt-token")
    monkeypatch.setattr(bitbucket_app.requests, "request", fail_if_called)
    monkeypatch.setattr(bitbucket_app.asyncio, "to_thread", fake_to_thread)

    with pytest.raises(bitbucket_app.requests.exceptions.Timeout):
        await bitbucket_app.get_bearer_token("shared-secret", "client-key")

    assert len(calls) == 1
    func, _, kwargs = calls[0]
    assert func is bitbucket_app.requests.request
    assert kwargs["timeout"] == 30


@pytest.mark.asyncio
async def test_push_validation_offloads_blocking_request(monkeypatch):
    commits_url = "https://api.bitbucket.org/2.0/repositories/acme/repo/pullrequests/1/commits"
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "values": [
                {
                    "author": {"user": {"display_name": "alice"}},
                    "date": "2026-08-27T12:00:00+00:00",
                }
            ]
        },
    )
    calls = []

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return response

    def fail_if_called(*args, **kwargs):
        pytest.fail("Blocking request was called directly")

    monkeypatch.setattr(
        bitbucket_app,
        "global_settings",
        _host_settings(),
    )
    monkeypatch.setattr(bitbucket_app.requests, "get", fail_if_called)
    monkeypatch.setattr(bitbucket_app.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        bitbucket_app,
        "context",
        SimpleNamespace(get=lambda key: "bearer-token"),
    )

    data = {
        "data": {
            "actor": {"username": "alice"},
            "pullrequest": {
                "links": {"commits": {"href": commits_url}},
                "updated_on": "2026-08-27T12:00:10+00:00",
            },
        }
    }

    assert await bitbucket_app._validate_time_from_last_commit_to_pr_update(data) is True
    assert len(calls) == 1
    func, args, kwargs = calls[0]
    assert func is bitbucket_app.requests.get
    assert args == (commits_url,)
    assert kwargs == {
        "headers": {
            "Authorization": "Bearer bearer-token",
            "Accept": "application/json",
        },
        "timeout": 30,
    }


@pytest.mark.asyncio
async def test_push_validation_handles_request_timeout(monkeypatch):
    calls = []

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        raise bitbucket_app.requests.exceptions.Timeout

    def fail_if_called(*args, **kwargs):
        pytest.fail("Blocking request was called directly")

    monkeypatch.setattr(
        bitbucket_app,
        "global_settings",
        _host_settings(),
    )
    monkeypatch.setattr(bitbucket_app.requests, "get", fail_if_called)
    monkeypatch.setattr(bitbucket_app.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        bitbucket_app,
        "context",
        SimpleNamespace(get=lambda key: "bearer-token"),
    )

    data = {
        "data": {
            "pullrequest": {
                "links": {"commits": {"href": "https://api.bitbucket.org/commits"}},
                "updated_on": "2026-08-27T12:00:10+00:00",
            },
        }
    }

    assert await bitbucket_app._validate_time_from_last_commit_to_pr_update(data) is False
    assert len(calls) == 1
    func, _, kwargs = calls[0]
    assert func is bitbucket_app.requests.get
    assert kwargs["timeout"] == 30
