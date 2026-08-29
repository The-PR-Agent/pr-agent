import os

import pytest

os.environ.setdefault("GITLAB__URL", "https://gitlab.example.com")
import pr_agent.servers.gitlab_webhook as gitlab_webhook


class FakeSecretProvider:
    """Stands in for a cloud secret client, which must not be shared across a fork."""


@pytest.fixture(autouse=True)
def clean_state():
    original = dict(gitlab_webhook._secret_provider_state)
    gitlab_webhook._secret_provider_state.clear()
    yield
    gitlab_webhook._secret_provider_state.clear()
    gitlab_webhook._secret_provider_state.update(original)


def test_nothing_is_built_at_import():
    # Under `preload_app` an import-time client would be built in the gunicorn master and
    # inherited by every worker, so the module must start with no provider at all.
    assert gitlab_webhook._secret_provider_state == {}


def test_builds_on_first_use(monkeypatch):
    provider = FakeSecretProvider()
    monkeypatch.setattr(gitlab_webhook, "get_secret_provider", lambda: provider)

    assert gitlab_webhook.get_fork_safe_secret_provider() is provider
    assert gitlab_webhook._secret_provider_state["pid"] == os.getpid()


def test_reuses_provider_within_the_same_process(monkeypatch):
    provider = FakeSecretProvider()
    gitlab_webhook._secret_provider_state.update({"provider": provider, "pid": os.getpid()})
    monkeypatch.setattr(gitlab_webhook, "get_secret_provider", lambda: pytest.fail("rebuilt without a fork"))

    assert gitlab_webhook.get_fork_safe_secret_provider() is provider


def test_rebuilds_provider_after_a_fork(monkeypatch):
    # A worker inheriting the parent's provider would share its pooled connection, so a
    # differing pid must force a fresh client.
    rebuilt = FakeSecretProvider()
    gitlab_webhook._secret_provider_state.update({"provider": FakeSecretProvider(), "pid": os.getpid() + 1})
    monkeypatch.setattr(gitlab_webhook, "get_secret_provider", lambda: rebuilt)

    assert gitlab_webhook.get_fork_safe_secret_provider() is rebuilt
    assert gitlab_webhook._secret_provider_state["pid"] == os.getpid()

    monkeypatch.setattr(gitlab_webhook, "get_secret_provider", lambda: pytest.fail("rebuilt twice"))
    assert gitlab_webhook.get_fork_safe_secret_provider() is rebuilt


def test_caches_none_when_no_provider_is_configured(monkeypatch):
    # get_secret_provider() returns None when CONFIG.SECRET_PROVIDER is unset; that answer
    # must be cached too, not retried on every webhook.
    calls = []

    def _build():
        calls.append(True)
        return None

    monkeypatch.setattr(gitlab_webhook, "get_secret_provider", _build)

    assert gitlab_webhook.get_fork_safe_secret_provider() is None
    assert gitlab_webhook.get_fork_safe_secret_provider() is None
    assert len(calls) == 1


@pytest.fixture
def gitlab_webhook_settings():
    """Snapshot and restore the whole GITLAB section, so a test cannot leak settings."""
    import copy as _copy

    from pr_agent.config_loader import get_settings

    settings = get_settings(use_context=False)
    original = _copy.deepcopy(settings.get("GITLAB", None))
    settings.set("GITLAB.SHARED_SECRET", "topsecret")
    settings.set("GITLAB.PERSONAL_ACCESS_TOKEN", "glpat-dummy")
    yield settings
    if original is not None:
        settings.set("GITLAB", original)


def _post_webhook(token=None):
    from fastapi import FastAPI
    from starlette.middleware import Middleware
    from starlette.testclient import TestClient
    from starlette_context.middleware import RawContextMiddleware

    app = FastAPI(middleware=[Middleware(RawContextMiddleware)])
    app.include_router(gitlab_webhook.router)
    headers = {"X-Gitlab-Token": token} if token is not None else {}
    return TestClient(app, raise_server_exceptions=False).post(
        "/webhook", json={"object_kind": "note", "event_type": "note"}, headers=headers)


def test_answer_a_wrong_shared_secret_with_401(gitlab_webhook_settings):
    """Answer a rejected delivery with 401 instead of the unconditional 200 that made a
    misconfigured token look healthy."""
    assert _post_webhook("wrong-secret").status_code == 401


def test_answer_a_missing_token_with_401(gitlab_webhook_settings):
    """Answer a delivery that carries no token at all with 401."""
    assert _post_webhook().status_code == 401


def test_accept_the_correct_shared_secret(gitlab_webhook_settings):
    """Accept a correctly authenticated delivery and dispatch it as before."""
    assert _post_webhook("topsecret").status_code == 200


def test_compare_the_shared_secret_in_constant_time(monkeypatch, gitlab_webhook_settings):
    """Compare the shared secret with a constant-time primitive, as every other webhook
    auth path in the project already does."""
    calls = []
    real_compare = gitlab_webhook.hmac.compare_digest

    def recording_compare(a, b):
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(gitlab_webhook.hmac, "compare_digest", recording_compare)

    _post_webhook("wrong-secret")

    assert calls, "hmac.compare_digest was not used to compare the shared secret"


def test_keep_the_webhook_token_out_of_the_logs(gitlab_webhook_settings):
    """Keep a rejected token out of the logs, where it would otherwise be shipped to a log
    aggregator in cleartext."""
    records = []
    handler_id = gitlab_webhook.get_logger().add(lambda m: records.append(str(m)))
    secret_token = "super-secret-webhook-token"
    try:
        _post_webhook(secret_token)
    finally:
        gitlab_webhook.get_logger().remove(handler_id)

    assert records, "nothing was logged, so the assertion below would be vacuous"
    assert not any(secret_token in record for record in records)
