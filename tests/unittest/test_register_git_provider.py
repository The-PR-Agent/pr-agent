"""register_git_provider makes an out-of-tree GitProvider selectable through config.git_provider."""

from types import SimpleNamespace
from typing import Optional

import pytest

from pr_agent import git_providers
from pr_agent.algo.types import FilePatchInfo
from pr_agent.git_providers import (
    _GIT_PROVIDERS,
    get_git_provider,
    get_git_provider_with_context,
    register_git_provider,
)
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.github_provider import GithubProvider

PR_URL = "https://forge.example/owner/repo/pulls/1"


class _ForgeProvider(GitProvider):
    """The smallest provider a package can register: concrete, so the registry can build it for a PR."""

    def __init__(self, pr_url: Optional[str] = None):
        self.pr_url = pr_url

    def is_supported(self, capability: str) -> bool:
        return False

    def get_files(self) -> list:
        return []

    def get_diff_files(self) -> list[FilePatchInfo]:
        return []

    def publish_description(self, pr_title: str, pr_body: str):
        pass

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        return False

    def get_languages(self):
        return {}

    def get_pr_branch(self):
        return "main"

    def get_user_id(self):
        return "forge"

    def get_pr_description_full(self) -> str:
        return ""

    def get_repo_settings(self):
        return b""

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        pass

    def publish_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str, original_suggestion=None):
        pass

    def publish_inline_comments(self, comments: list[dict]):
        pass

    def remove_initial_comment(self):
        pass

    def remove_comment(self, comment):
        pass

    def get_issue_comments(self):
        return []

    def publish_labels(self, labels):
        pass

    def get_pr_labels(self, update=False):
        return []

    def add_eyes_reaction(self, issue_comment_id: int, disable_eyes: bool = False) -> Optional[int]:
        return None

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        return False

    def get_commit_messages(self) -> str:
        return ""


class _OtherForgeProvider(_ForgeProvider):
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


def test_registered_provider_is_built_for_a_pr_url(registry, monkeypatch):
    """The path every tool takes: apply_repo_settings resolves the provider through
    get_git_provider_with_context, which instantiates the registered class."""
    register_git_provider("forge", _ForgeProvider)
    monkeypatch.setattr(git_providers, "get_settings", lambda: _settings_selecting("forge"))

    provider = get_git_provider_with_context(PR_URL)

    assert isinstance(provider, _ForgeProvider)
    assert provider.pr_url == PR_URL


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
