"""`config.global_settings_repo` decides which repository supplies namespace-wide settings.

Before #3890 every provider resolved `<namespace>/pr-agent-settings` by convention, so
anyone able to create a repository inside a namespace could set the configuration applied to
every repository in it. These tests pin the explicit, host-only opt-in that replaces it.
"""
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.config_security import CLI_HOST_ONLY_KEYS_BY_SECTION, REPO_HOST_ONLY_KEYS_BY_SECTION
from pr_agent.git_providers import git_provider as gp
from pr_agent.git_providers.github_provider import GithubProvider


@pytest.fixture(autouse=True)
def _clear_global_settings_cache():
    gp._GLOBAL_SETTINGS_CACHE.clear()
    yield
    gp._GLOBAL_SETTINGS_CACHE.clear()


class _Repo:
    def __init__(self, content=b""):
        self.content = content

    def get_contents(self, path, ref=None):
        return MagicMock(decoded_content=self.content)


class _Client:
    def __init__(self, repos):
        self.repos = repos
        self.requested = []

    def get_repo(self, name):
        self.requested.append(name)
        if name not in self.repos:
            raise AssertionError(f"unexpected lookup of {name!r}")
        return self.repos[name]


def _provider(repos):
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "acme/widgets"
    provider.github_client = _Client(repos)
    return provider


@contextmanager
def _settings(**values):
    settings = get_settings()
    originals = {key: getattr(settings.config, key) for key in values}
    for key, value in values.items():
        setattr(settings.config, key, value)
    try:
        yield
    finally:
        for key, value in originals.items():
            setattr(settings.config, key, value)


def test_no_repository_is_resolved_when_unset():
    """The default is empty: the feature is off, so no repository is adopted by name."""
    provider = _provider({"acme/pr-agent-settings": _Repo(b"[pr_reviewer]\n")})

    with _settings(use_global_settings_file=True, global_settings_repo=""):
        assert provider._get_global_repo_settings() == ""

    assert provider.github_client.requested == []


def test_configured_repository_is_read():
    provider = _provider({"acme/settings": _Repo(b"[pr_reviewer]\n")})

    with _settings(use_global_settings_file=True, global_settings_repo="settings"):
        assert provider._get_global_repo_settings() == b"[pr_reviewer]\n"

    assert provider.github_client.requested == ["acme/settings"]


def test_qualified_name_resolves_the_same_repository():
    provider = _provider({"acme/settings": _Repo(b"[pr_reviewer]\n")})

    with _settings(use_global_settings_file=True, global_settings_repo="acme/settings"):
        assert provider._get_global_repo_settings() == b"[pr_reviewer]\n"


@pytest.mark.parametrize("configured", [
    "other-org/settings",   # a different namespace
    "../settings",          # traversal
    "acme/../other",        # traversal after the namespace
    "acme//settings",       # empty segment
    "acme/settings/extra",  # too many segments
    "https://evil.example/settings",
    "settings?x=1",
])
def test_rejected_names_read_nothing(configured):
    provider = _provider({"acme/pr-agent-settings": _Repo(b"[pr_reviewer]\n")})

    with _settings(use_global_settings_file=True, global_settings_repo=configured):
        assert provider._get_global_repo_settings() == ""

    assert provider.github_client.requested == []


@pytest.mark.parametrize("configured", [None, 5, ["settings"]])
def test_non_string_values_are_ignored(configured):
    provider = _provider({"acme/pr-agent-settings": _Repo(b"[pr_reviewer]\n")})

    with _settings(use_global_settings_file=True, global_settings_repo=configured):
        assert provider._get_global_repo_settings() == ""

    assert provider.github_client.requested == []


def test_disabled_flag_still_wins_over_a_configured_repository():
    provider = _provider({"acme/settings": _Repo(b"[pr_reviewer]\n")})

    with _settings(use_global_settings_file=False, global_settings_repo="settings"):
        assert provider._get_global_repo_settings() == ""

    assert provider.github_client.requested == []


def test_changing_the_configured_repository_is_not_served_from_cache():
    """The TTL cache is keyed per namespace; the repository name has to be part of that key."""
    provider = _provider({
        "acme/one": _Repo(b"[pr_reviewer]\nextra_instructions = \"one\"\n"),
        "acme/two": _Repo(b"[pr_reviewer]\nextra_instructions = \"two\"\n"),
    })

    with _settings(use_global_settings_file=True, global_settings_repo="one"):
        first = provider._get_global_repo_settings()
    with _settings(use_global_settings_file=True, global_settings_repo="two"):
        second = provider._get_global_repo_settings()

    assert b"one" in first
    assert b"two" in second


def test_the_setting_is_host_only():
    """Repository settings and comment arguments must not repoint the namespace lookup."""
    assert "global_settings_repo" in REPO_HOST_ONLY_KEYS_BY_SECTION["config"]
    assert "global_settings_repo" in CLI_HOST_ONLY_KEYS_BY_SECTION["config"]
