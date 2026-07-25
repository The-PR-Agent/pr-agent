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
