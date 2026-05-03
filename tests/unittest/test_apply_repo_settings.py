import copy

import pytest
from starlette_context import context, request_cycle_context

from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers import utils as git_utils

REPO_A_TOML = b"""
[pr_reviewer]
extra_instructions = "MARKER-FROM-REPO-A"

[pr_code_suggestions]
extra_instructions = "MARKER-FROM-REPO-A"
"""


class FakeGitProvider:
    def __init__(self, repo_settings: bytes):
        self._repo_settings = repo_settings

    def get_repo_settings(self):
        return self._repo_settings

    def is_supported(self, feature):
        return False

    def publish_comment(self, body):
        pass

    def publish_persistent_comment(self, *args, **kwargs):
        pass


@pytest.fixture
def fresh_global_settings():
    """Restore module-level global_settings after each test in case anything mutated it."""
    snapshot = copy.deepcopy(global_settings.as_dict())
    yield
    for section in set(global_settings.as_dict().keys()) - set(snapshot.keys()):
        global_settings.unset(section)
    for section, contents in snapshot.items():
        global_settings.unset(section)
        global_settings.set(section, copy.deepcopy(contents), merge=False)


def _extra_instructions(section: str) -> str:
    return get_settings().get(f"{section}.extra_instructions", "") or ""


class TestApplyRepoSettings:
    """Verify that the per-request settings clone (set by webhook handlers via
    `context['settings'] = copy.deepcopy(global_settings)`) successfully
    isolates `apply_repo_settings()` mutations to the request that produced
    them — preventing cross-repo `.pr_agent.toml` state leaks reported in #2345.
    """

    def test_repo_settings_from_toml_are_applied(self, fresh_global_settings, monkeypatch):
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda url: FakeGitProvider(REPO_A_TOML),
        )
        with request_cycle_context({}):
            context["settings"] = copy.deepcopy(global_settings)
            git_utils.apply_repo_settings("https://git.example/projects/A/repos/a/pull-requests/1")
            assert "MARKER-FROM-REPO-A" in _extra_instructions("pr_reviewer")
            assert "MARKER-FROM-REPO-A" in _extra_instructions("pr_code_suggestions")

    def test_repo_without_toml_does_not_inherit_previous_repo_settings(
        self, fresh_global_settings, monkeypatch
    ):
        # Request 1: Repo A with .pr_agent.toml — mutates only this request's settings clone.
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda url: FakeGitProvider(REPO_A_TOML),
        )
        with request_cycle_context({}):
            context["settings"] = copy.deepcopy(global_settings)
            git_utils.apply_repo_settings("https://git.example/projects/A/repos/a/pull-requests/1")
            assert "MARKER-FROM-REPO-A" in _extra_instructions("pr_reviewer"), "precondition"

        # Request 2: Repo B with no .pr_agent.toml — fresh clone of the unmutated global_settings.
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda url: FakeGitProvider(b""),
        )
        with request_cycle_context({}):
            context["settings"] = copy.deepcopy(global_settings)
            git_utils.apply_repo_settings("https://git.example/projects/B/repos/b/pull-requests/1")
            assert "MARKER-FROM-REPO-A" not in _extra_instructions("pr_reviewer"), \
                "repo A's [pr_reviewer].extra_instructions leaked into repo B"
            assert "MARKER-FROM-REPO-A" not in _extra_instructions("pr_code_suggestions"), \
                "repo A's [pr_code_suggestions].extra_instructions leaked into repo B"

    def test_unknown_section_does_not_leak_to_next_repo(self, fresh_global_settings, monkeypatch):
        """Catches the case where a repo's `.pr_agent.toml` introduces a section
        name not present in the startup defaults. With the per-request clone,
        the new section lives in `context['settings']` and dies with the request.
        """
        custom_section_toml = b"""
[my_custom_repo_section]
foo = "X-FROM-REPO-A"
"""
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda url: FakeGitProvider(custom_section_toml),
        )
        with request_cycle_context({}):
            context["settings"] = copy.deepcopy(global_settings)
            git_utils.apply_repo_settings("https://git.example/projects/A/repos/a/pull-requests/1")
            assert get_settings().get("my_custom_repo_section.foo") == "X-FROM-REPO-A", "precondition"

        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda url: FakeGitProvider(b""),
        )
        with request_cycle_context({}):
            context["settings"] = copy.deepcopy(global_settings)
            git_utils.apply_repo_settings("https://git.example/projects/B/repos/b/pull-requests/1")
            assert get_settings().get("my_custom_repo_section.foo") is None, \
                "repo A's [my_custom_repo_section] leaked into repo B"
