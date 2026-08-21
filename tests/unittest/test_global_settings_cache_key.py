"""The global-settings cache is process-wide, so its key must identify the instance."""
import inspect

import pytest

from pr_agent.git_providers import git_provider as gp
from pr_agent.git_providers import github_provider as ghp


@pytest.fixture(autouse=True)
def clear_cache():
    gp._GLOBAL_SETTINGS_CACHE.clear()
    yield
    gp._GLOBAL_SETTINGS_CACHE.clear()


class SettingsStub:
    class config:
        use_global_settings_file = True

    def get(self, key, default=None):
        return default


def _record_keys(monkeypatch, module):
    keys = []

    def fake_cache(key, fetch):
        keys.append(key)
        return ""

    monkeypatch.setattr(module, "get_cached_global_settings", fake_cache)
    monkeypatch.setattr(module, "get_settings", lambda: SettingsStub())
    return keys


def _github(base_url):
    provider = ghp.GithubProvider.__new__(ghp.GithubProvider)
    provider.base_url = base_url
    provider.repo = "acme/widgets"
    provider.github_client = object()
    return provider


def test_the_same_org_on_two_hosts_does_not_share_an_entry():
    """Two instances hosting the same org name must not read each other's settings."""
    a = gp.get_cached_global_settings("github:https://github.com:acme", lambda: "from-dot-com")
    b = gp.get_cached_global_settings("github:https://ghe.internal:acme", lambda: "from-ghe")

    assert a == "from-dot-com"
    assert b == "from-ghe"


def test_the_same_key_is_still_cached():
    """Caching must still work for repeated lookups of the same instance and org."""
    calls = []

    def fetch():
        calls.append(1)
        return "v"

    gp.get_cached_global_settings("github:https://github.com:acme", fetch)
    gp.get_cached_global_settings("github:https://github.com:acme", fetch)

    assert len(calls) == 1


def test_github_keys_the_cache_on_the_host(monkeypatch):
    """The same org on github.com and on an enterprise host must key differently."""
    keys = _record_keys(monkeypatch, ghp)

    _github("https://api.github.com")._get_global_repo_settings()
    _github("https://ghe.internal/api/v3")._get_global_repo_settings()

    assert len(set(keys)) == 2
    assert all("acme" in key for key in keys)


def test_the_bare_org_only_key_is_gone():
    """The org-only key must not survive anywhere in the provider."""
    assert 'f"github:{repo_owner}"' not in inspect.getsource(ghp)
