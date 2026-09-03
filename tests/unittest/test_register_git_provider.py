"""register_git_provider makes an out-of-tree GitProvider selectable through config.git_provider."""

from types import SimpleNamespace

import pytest

from pr_agent import git_providers
from pr_agent.git_providers import _GIT_PROVIDERS, get_git_provider, register_git_provider
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.github_provider import GithubProvider


class _ForgeProvider(GitProvider):
    pass  # the registry stores the class and never instantiates it, so the abstract methods can stay


class _OtherForgeProvider(GitProvider):
    pass


@pytest.fixture
def registry():
    before = dict(_GIT_PROVIDERS)
    yield _GIT_PROVIDERS
    _GIT_PROVIDERS.clear()
    _GIT_PROVIDERS.update(before)


def _settings_selecting(provider_id):
    return SimpleNamespace(config=SimpleNamespace(git_provider=provider_id), get=lambda key, default=None: default)


def test_registered_provider_is_selected_through_settings(registry, monkeypatch):
    register_git_provider("forge", _ForgeProvider)
    monkeypatch.setattr(git_providers, "get_settings", lambda: _settings_selecting("forge"))

    assert get_git_provider() is _ForgeProvider


def test_registering_the_same_class_again_is_a_no_op(registry):
    register_git_provider("forge", _ForgeProvider)
    register_git_provider("forge", _ForgeProvider)

    assert registry["forge"] is _ForgeProvider


def test_a_taken_id_is_not_shadowed(registry):
    register_git_provider("forge", _ForgeProvider)

    with pytest.raises(ValueError, match="already registered"):
        register_git_provider("forge", _OtherForgeProvider)
    with pytest.raises(ValueError, match="already registered"):
        register_git_provider("github", _ForgeProvider)

    assert registry["forge"] is _ForgeProvider
    assert registry["github"] is GithubProvider


@pytest.mark.parametrize("not_a_provider", [object, GithubProvider.__new__(GithubProvider), "forge"])
def test_only_git_provider_subclasses_are_accepted(registry, not_a_provider):
    with pytest.raises(TypeError, match="GitProvider subclass"):
        register_git_provider("forge", not_a_provider)

    assert "forge" not in registry
