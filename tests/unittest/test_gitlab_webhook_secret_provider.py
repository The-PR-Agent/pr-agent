import json
import os
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("GITLAB__URL", "https://gitlab.example.com")
import pr_agent.servers.gitlab_webhook as gitlab_webhook


class FakeSecretProvider:
    """Stands in for a cloud secret client, which must not be shared across a fork."""

    def __init__(self, secret=None, error=None):
        self.secret = secret
        self.error = error
        self.lookups = []

    def get_secret(self, token):
        self.lookups.append(token)
        if self.error is not None:
            raise RuntimeError(self.error)
        return self.secret


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


async def _post_webhook(token=None):
    from fastapi import FastAPI
    from starlette.middleware import Middleware
    from starlette_context.middleware import RawContextMiddleware

    app = FastAPI(middleware=[Middleware(RawContextMiddleware)])
    app.include_router(gitlab_webhook.router)
    headers = {"X-Gitlab-Token": token} if token is not None else {}
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(
            "/webhook", json={"object_kind": "note", "event_type": "note"}, headers=headers
        )


@pytest.mark.asyncio
async def test_answer_a_wrong_shared_secret_with_401(gitlab_webhook_settings):
    """Answer a rejected delivery with 401 instead of the unconditional 200 that made a
    misconfigured token look healthy."""
    assert (await _post_webhook("wrong-secret")).status_code == 401


@pytest.mark.asyncio
async def test_answer_a_missing_token_with_401(gitlab_webhook_settings):
    """Answer a delivery that carries no token at all with 401."""
    assert (await _post_webhook()).status_code == 401


@pytest.mark.asyncio
async def test_accept_the_correct_shared_secret(gitlab_webhook_settings):
    """Accept a correctly authenticated delivery and dispatch it as before."""
    assert (await _post_webhook("topsecret")).status_code == 200


@pytest.mark.asyncio
async def test_compare_the_shared_secret_in_constant_time(monkeypatch, gitlab_webhook_settings):
    """Compare the shared secret with a constant-time primitive, as every other webhook
    auth path in the project already does."""
    calls = []
    real_compare = gitlab_webhook.hmac.compare_digest

    def recording_compare(a, b):
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(gitlab_webhook.hmac, "compare_digest", recording_compare)

    await _post_webhook("wrong-secret")

    assert calls, "hmac.compare_digest was not used to compare the shared secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("secret_token", ["super-secret-webhook-token", "project-secret:super-secret-webhook-token"])
async def test_keep_the_webhook_token_out_of_the_logs(monkeypatch, gitlab_webhook_settings, secret_token):
    """Keep a rejected token out of the logs, where it would otherwise be shipped to a log
    aggregator in cleartext."""
    records = []
    handler_id = gitlab_webhook.get_logger().add(lambda m: records.append(str(m)))
    secret = json.dumps({"gitlab_token": "glpat-private-test", "webhook_token": "stored-private-test"})
    monkeypatch.setattr(gitlab_webhook, "get_secret_provider", lambda: FakeSecretProvider(secret=secret))
    try:
        await _post_webhook(secret_token)
    finally:
        gitlab_webhook.get_logger().remove(handler_id)

    assert records, "nothing was logged, so the assertion below would be vacuous"
    assert not any(value in record for record in records
                   for value in [secret_token, "project-secret", "glpat-private-test", "stored-private-test"])


@pytest.mark.asyncio
async def test_accept_a_webhook_token_resolved_by_the_secret_provider(monkeypatch, gitlab_webhook_settings):
    """Accept a delivery whose token resolves through the cloud secret provider even when it
    does not match the configured shared secret."""
    secret = json.dumps({
        "gitlab_token": "glpat-provider", "webhook_token": "provider-token", "token_name": "webhook-1"
    })
    provider = FakeSecretProvider(secret=secret)
    monkeypatch.setattr(gitlab_webhook, "get_secret_provider", lambda: provider)

    assert (await _post_webhook("project-secret:provider-token")).status_code == 200
    assert provider.lookups == ["project-secret"]


@pytest.mark.asyncio
async def test_shared_secret_skips_provider_construction(monkeypatch, gitlab_webhook_settings):
    monkeypatch.setattr(gitlab_webhook, "get_fork_safe_secret_provider",
                        lambda: pytest.fail("shared-secret authentication consulted the cloud provider"))

    assert (await _post_webhook("topsecret")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "", "project-secret", ":provider-token", "project-secret:"])
async def test_reject_incomplete_provider_credentials_without_lookup(monkeypatch, gitlab_webhook_settings, token):
    monkeypatch.setattr(gitlab_webhook, "get_fork_safe_secret_provider",
                        lambda: pytest.fail("incomplete credential consulted the cloud provider"))

    assert (await _post_webhook(token)).status_code == 401


@pytest.mark.parametrize("secret", [
    "not-json", "[]", "null",
    '{"gitlab_token": "glpat-provider"}',
    '{"gitlab_token": "glpat-provider", "webhook_token": "wrong"}',
    '{"gitlab_token": "glpat-provider", "webhook_token": ""}',
    '{"gitlab_token": "glpat-provider", "webhook_token": 123}',
    '{"gitlab_token": "", "webhook_token": "provider-token"}',
    '{"gitlab_token": 123, "webhook_token": "provider-token"}',
    '{"webhook_token": "provider-token"}',
])
def test_reject_invalid_provider_credentials_without_installing_pat(monkeypatch, gitlab_webhook_settings, secret):
    import copy

    from starlette.requests import Request
    from starlette_context import context, request_cycle_context

    provider = FakeSecretProvider(secret=secret)
    monkeypatch.setattr(gitlab_webhook, "get_secret_provider", lambda: provider)
    request = Request({"type": "http", "headers": [(b"x-gitlab-token", b"project-secret:provider-token")]})
    log_context = {}
    with request_cycle_context({"settings": copy.deepcopy(gitlab_webhook_settings)}):
        response = gitlab_webhook.authenticate_gitlab_webhook(request, log_context)
        assert response.status_code == 401
        assert context["settings"].gitlab.personal_access_token == "glpat-dummy"
    assert log_context == {}


def test_compare_provider_token_before_installing_request_credentials(monkeypatch, gitlab_webhook_settings):
    import copy

    from starlette.requests import Request
    from starlette_context import context, request_cycle_context

    secrets = {
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:project-one":
            json.dumps({"gitlab_token": "glpat-one", "webhook_token": "token-one", "token_name": "project-one"}),
        "project-two": json.dumps({"gitlab_token": "glpat-two", "webhook_token": "token-two"}),
    }
    lookups = []
    comparisons = []
    real_compare = gitlab_webhook.hmac.compare_digest

    def get_secret(name):
        lookups.append(name)
        return secrets[name]

    def compare(left, right):
        comparisons.append((left, right))
        assert context["settings"].gitlab.personal_access_token == "glpat-dummy"
        return real_compare(left, right)

    monkeypatch.setattr(gitlab_webhook, "get_fork_safe_secret_provider", lambda: SimpleNamespace(get_secret=get_secret))
    monkeypatch.setattr(gitlab_webhook.hmac, "compare_digest", compare)
    for name, token, expected_pat in [
        (next(iter(secrets)), "token-one", "glpat-one"),
        ("project-two", "token-two", "glpat-two"),
    ]:
        request = Request({"type": "http", "headers": [(b"x-gitlab-token", f"{name}:{token}".encode())]})
        with request_cycle_context({"settings": copy.deepcopy(gitlab_webhook_settings)}):
            assert gitlab_webhook.authenticate_gitlab_webhook(request, {}) is None
            assert context["settings"].gitlab.personal_access_token == expected_pat

    assert lookups == list(secrets)
    assert (b"token-one", b"token-one") in comparisons
    assert (b"token-two", b"token-two") in comparisons
    assert gitlab_webhook_settings.gitlab.personal_access_token == "glpat-dummy"


@pytest.mark.asyncio
async def test_keep_shared_secret_available_when_provider_initialization_fails(monkeypatch, gitlab_webhook_settings):
    """Authenticate the shared secret without needing an unavailable cloud client."""
    monkeypatch.setattr(gitlab_webhook, "get_secret_provider",
                        lambda: (_ for _ in ()).throw(RuntimeError("secrets manager unreachable")))

    assert (await _post_webhook("topsecret")).status_code == 200
    assert (await _post_webhook("project-secret:wrong-secret")).status_code == 401


@pytest.mark.asyncio
async def test_keep_shared_secret_available_when_the_secret_read_fails(monkeypatch, gitlab_webhook_settings):
    """Accept the shared secret and reject a provider credential whose lookup raises."""
    monkeypatch.setattr(
        gitlab_webhook, "get_secret_provider",
        lambda: FakeSecretProvider(error="secrets manager read failed"))

    assert (await _post_webhook("topsecret")).status_code == 200
    assert (await _post_webhook("project-secret:wrong-secret")).status_code == 401


@pytest.mark.asyncio
async def test_accept_shared_secret_and_reject_empty_provider_lookup(monkeypatch, gitlab_webhook_settings):
    """Accept the shared secret and reject an empty lookup, as returned during a read outage."""
    monkeypatch.setattr(gitlab_webhook, "get_secret_provider", lambda: FakeSecretProvider(secret=""))

    assert (await _post_webhook("topsecret")).status_code == 200
    assert (await _post_webhook("project-secret:unseen-token")).status_code == 401


@pytest.mark.asyncio
async def test_do_not_leak_provider_exception_details_in_the_failure_warning(monkeypatch, gitlab_webhook_settings):
    """Keep provider exception text out of the failure warning, as the providers themselves
    already redact it and the webhook logs are shipped to aggregators."""
    records = []
    handler_id = gitlab_webhook.get_logger().add(lambda m: records.append(str(m)))
    try:
        monkeypatch.setattr(gitlab_webhook, "get_secret_provider",
                            lambda: (_ for _ in ()).throw(RuntimeError("credential-process diagnostics")))
        assert (await _post_webhook("project-secret:any-token")).status_code == 401
    finally:
        gitlab_webhook.get_logger().remove(handler_id)

    assert records, "nothing was logged, so the assertion below would be vacuous"
    assert not any("credential-process diagnostics" in record for record in records)
    assert any("RuntimeError" in record and "Secret provider failed" in record for record in records)


@pytest.mark.asyncio
async def test_degrade_to_401_when_provider_fails_and_no_shared_secret(monkeypatch, gitlab_webhook_settings):
    """Fail closed with 401 when the provider fails and no shared secret is configured."""
    settings = gitlab_webhook_settings
    settings.set("GITLAB.SHARED_SECRET", "")
    monkeypatch.setattr(gitlab_webhook, "get_secret_provider",
                        lambda: (_ for _ in ()).throw(RuntimeError("secrets manager unreachable")))

    assert (await _post_webhook("project-secret:any-token")).status_code == 401


@pytest.mark.asyncio
async def test_reject_a_token_unknown_to_provider_and_shared_secret(monkeypatch, gitlab_webhook_settings):
    """Reject a token that neither the provider nor the shared secret recognizes."""
    monkeypatch.setattr(gitlab_webhook, "get_secret_provider", lambda: FakeSecretProvider(secret=""))

    assert (await _post_webhook("project-secret:unseen-token")).status_code == 401
