"""Build the secret provider per process, never at import, since gunicorn preloads the app."""
import importlib
import os
import sys

import pytest

import pr_agent.secret_providers as secret_providers
import pr_agent.servers.bitbucket_app as bitbucket_app


class FakeSecretProvider:
    """Stand in for a cloud secret client, which must not be shared across a fork."""


class SettingsStub:
    def __init__(self, secret_provider):
        self._secret_provider = secret_provider

    def get(self, key, default=None):
        if key == "CONFIG.SECRET_PROVIDER":
            return self._secret_provider
        return default


@pytest.fixture(autouse=True)
def clean_state():
    original = dict(bitbucket_app._secret_provider_state)
    bitbucket_app._secret_provider_state.clear()
    yield
    bitbucket_app._secret_provider_state.clear()
    bitbucket_app._secret_provider_state.update(original)


def test_nothing_is_built_at_import():
    assert bitbucket_app._secret_provider_state == {}


def test_builds_on_first_use(monkeypatch):
    provider = FakeSecretProvider()
    monkeypatch.setattr(bitbucket_app, "get_secret_provider", lambda: provider)
    monkeypatch.setattr(bitbucket_app, "get_settings",
                        lambda: SettingsStub("google_cloud_storage"))

    assert bitbucket_app.get_fork_safe_secret_provider() is provider
    assert bitbucket_app.get_fork_safe_secret_provider() is provider


def test_a_provider_from_another_process_is_not_adopted(monkeypatch):
    built = []

    def build():
        provider = FakeSecretProvider()
        built.append(provider)
        return provider

    monkeypatch.setattr(bitbucket_app, "get_secret_provider", build)
    monkeypatch.setattr(bitbucket_app, "get_settings",
                        lambda: SettingsStub("google_cloud_storage"))
    inherited = FakeSecretProvider()
    bitbucket_app._secret_provider_state.update({"pid": os.getpid() + 1, "provider": inherited})

    returned = bitbucket_app.get_fork_safe_secret_provider()

    assert returned is built[0]
    assert returned is not inherited
    assert bitbucket_app._secret_provider_state["pid"] == os.getpid()


def test_no_provider_configured_returns_none(monkeypatch):
    monkeypatch.setattr(bitbucket_app, "get_settings", lambda: SettingsStub(None))

    assert bitbucket_app.get_fork_safe_secret_provider() is None


def test_importing_the_module_validates_the_setting(monkeypatch):
    """Fail at startup on a typo, which the removed import-time client used to catch."""
    monkeypatch.setattr(secret_providers, "get_settings",
                        lambda: SettingsStub("not_a_real_provider"))
    monkeypatch.delitem(sys.modules, bitbucket_app.__name__, raising=False)

    with pytest.raises(ValueError):
        importlib.import_module(bitbucket_app.__name__)


def test_importing_the_module_accepts_a_known_provider(monkeypatch):
    """Import cleanly for a supported provider, without building its client."""
    built = []
    monkeypatch.setattr(secret_providers, "get_settings",
                        lambda: SettingsStub("google_cloud_storage"))
    monkeypatch.setattr(secret_providers, "get_secret_provider",
                        lambda: built.append(1) or FakeSecretProvider())
    monkeypatch.delitem(sys.modules, bitbucket_app.__name__, raising=False)

    reloaded = importlib.import_module(bitbucket_app.__name__)

    assert reloaded._secret_provider_state == {}
    assert built == []
