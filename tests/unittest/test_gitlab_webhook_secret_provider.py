import os

import pytest

os.environ.setdefault("GITLAB__URL", "https://gitlab.example.com")
import pr_agent.servers.gitlab_webhook as gitlab_webhook


class FakeSecretProvider:
    """Stands in for a cloud secret client, which must not be shared across a fork."""


@pytest.fixture(autouse=True)
def restore_state():
    original = dict(gitlab_webhook._secret_provider_state)
    yield
    gitlab_webhook._secret_provider_state.clear()
    gitlab_webhook._secret_provider_state.update(original)


def test_reuses_provider_within_the_same_process(monkeypatch):
    provider = FakeSecretProvider()
    gitlab_webhook._secret_provider_state.update({"provider": provider, "pid": os.getpid()})
    monkeypatch.setattr(gitlab_webhook, "_build_secret_provider", lambda: pytest.fail("rebuilt without a fork"))

    assert gitlab_webhook.get_fork_safe_secret_provider() is provider


def test_rebuilds_provider_after_a_fork(monkeypatch):
    # A worker forked from the gunicorn master inherits the master's provider, and with it
    # the master's pooled connection. A differing pid must force a fresh client.
    rebuilt = FakeSecretProvider()
    gitlab_webhook._secret_provider_state.update({"provider": FakeSecretProvider(), "pid": os.getpid() + 1})
    monkeypatch.setattr(gitlab_webhook, "_build_secret_provider", lambda: rebuilt)

    assert gitlab_webhook.get_fork_safe_secret_provider() is rebuilt
    assert gitlab_webhook._secret_provider_state["pid"] == os.getpid()
    # The rebuild is claimed for this process, so a second call must not build again.
    monkeypatch.setattr(gitlab_webhook, "_build_secret_provider", lambda: pytest.fail("rebuilt twice"))
    assert gitlab_webhook.get_fork_safe_secret_provider() is rebuilt


def test_stays_none_when_no_provider_is_configured(monkeypatch):
    # Nothing to rebuild when secrets come from config rather than a cloud provider.
    gitlab_webhook._secret_provider_state.update({"provider": None, "pid": os.getpid() + 1})
    monkeypatch.setattr(gitlab_webhook, "_build_secret_provider", lambda: pytest.fail("built a provider"))

    assert gitlab_webhook.get_fork_safe_secret_provider() is None
