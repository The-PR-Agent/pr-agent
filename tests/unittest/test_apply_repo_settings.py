import copy

import pytest
from starlette_context import context, request_cycle_context

from pr_agent.config_loader import (
    _APPLIED_REPO_OVERRIDES,
    get_settings,
    global_settings,
    note_repo_setting_override,
    reset_repo_settings_overrides,
)
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


@pytest.fixture
def clean_repo_overrides():
    """Isolate the module-level repo-override ledger around each test so recorded
    overrides don't carry across tests."""
    saved = dict(_APPLIED_REPO_OVERRIDES)
    _APPLIED_REPO_OVERRIDES.clear()
    yield
    _APPLIED_REPO_OVERRIDES.clear()
    _APPLIED_REPO_OVERRIDES.update(saved)


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


class TestRepoSettingsOverrideRevertWithoutClone:
    """Verify the override-revert safety net in `apply_repo_settings()` itself.

    The tests above cover callers that install a per-request `context['settings']`
    clone (every webhook server). These tests deliberately run WITHOUT that clone,
    so `get_settings()` returns the shared `global_settings` — the situation for
    long-running non-webhook callers such as `github_polling`, which processes many
    PRs in one process. There, cross-repo leaks are prevented by reverting exactly
    the keys the previous repo's `.pr_agent.toml` overrode.
    """

    def test_leak_reverted_without_request_clone(
        self, fresh_global_settings, clean_repo_overrides, monkeypatch
    ):
        """Repo A's extra_instructions must not survive into repo B when no
        per-request clone isolates the two loads (issue #2345)."""
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda url: FakeGitProvider(REPO_A_TOML),
        )
        git_utils.apply_repo_settings("https://git.example/projects/A/repos/a/pull-requests/1")
        assert "MARKER-FROM-REPO-A" in _extra_instructions("pr_reviewer"), "precondition"

        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda url: FakeGitProvider(b""),
        )
        git_utils.apply_repo_settings("https://git.example/projects/B/repos/b/pull-requests/1")

        assert "MARKER-FROM-REPO-A" not in _extra_instructions("pr_reviewer"), \
            "repo A's [pr_reviewer].extra_instructions leaked into repo B"
        assert "MARKER-FROM-REPO-A" not in _extra_instructions("pr_code_suggestions"), \
            "repo A's [pr_code_suggestions].extra_instructions leaked into repo B"

    def test_runtime_flags_not_reverted(
        self, fresh_global_settings, clean_repo_overrides, monkeypatch
    ):
        """config.is_auto_command / config.is_new_pr are set outside the repo-settings
        merge, so the revert on the second apply_repo_settings() (from
        PRAgent._handle_request) must leave them intact — auto-command flows depend on it."""
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda url: FakeGitProvider(REPO_A_TOML),
        )
        git_utils.apply_repo_settings("https://git.example/projects/A/repos/a/pull-requests/1")
        # Server sets request-scoped runtime flags after the first apply.
        get_settings().set("config.is_auto_command", True)
        get_settings().set("config.is_new_pr", False)

        # Second apply for a repo without .pr_agent.toml (e.g. the per-command re-apply).
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda url: FakeGitProvider(b""),
        )
        git_utils.apply_repo_settings("https://git.example/projects/B/repos/b/pull-requests/1")

        assert get_settings().config.is_auto_command is True, \
            "is_auto_command was wiped by the second apply_repo_settings()"
        assert get_settings().config.is_new_pr is False, \
            "is_new_pr was wiped by the second apply_repo_settings()"
        # ...while the actual repo-settings leak is still fixed.
        assert "MARKER-FROM-REPO-A" not in _extra_instructions("pr_reviewer")

    def test_non_repo_config_is_not_reverted(
        self, fresh_global_settings, clean_repo_overrides, monkeypatch
    ):
        """Base/runtime config set outside the repo-settings merge (e.g. an operator's
        config.extra_config_url) must survive a subsequent apply_repo_settings()."""
        get_settings().set("config.extra_config_url", "")  # ensure no real fetch
        get_settings().set("config.output_relevant_configurations", True)

        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda url: FakeGitProvider(REPO_A_TOML),
        )
        git_utils.apply_repo_settings("https://git.example/projects/A/repos/a/pull-requests/1")
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda url: FakeGitProvider(b""),
        )
        git_utils.apply_repo_settings("https://git.example/projects/B/repos/b/pull-requests/1")

        assert get_settings().config.output_relevant_configurations is True, \
            "a non-repo config value was clobbered by the repo-settings revert"

    def test_revert_targets_effective_settings_object(
        self, fresh_global_settings, clean_repo_overrides, monkeypatch
    ):
        """Record/revert must operate on the object get_settings() returns (e.g. a
        per-request context["settings"] clone), never only global_settings."""
        clone = copy.deepcopy(global_settings)
        monkeypatch.setattr("pr_agent.config_loader.get_settings", lambda *a, **k: clone)

        # Simulate a repo override recorded + written on the effective (clone) object.
        note_repo_setting_override("pr_reviewer", "extra_instructions")
        clone.set("pr_reviewer.extra_instructions", "LEAK-ON-CLONE")
        assert clone.get("pr_reviewer.extra_instructions") == "LEAK-ON-CLONE"

        reset_repo_settings_overrides()

        assert clone.get("pr_reviewer.extra_instructions") != "LEAK-ON-CLONE", \
            "revert did not operate on the effective (context clone) settings object"
        assert global_settings.get("pr_reviewer.extra_instructions") != "LEAK-ON-CLONE", \
            "global_settings must be untouched when the effective object is a clone"

    def test_claude_shorthand_model_keys_do_not_leak(
        self, fresh_global_settings, clean_repo_overrides, monkeypatch
    ):
        """A repo selecting the 'claude-3-5-sonnet' shorthand triggers set_claude_model(),
        which also rewrites config.model_weak / config.fallback_models. Those derived keys
        must be reverted (not just config.model) for a later repo that doesn't use it."""
        baseline_model_weak = get_settings().get("config.model_weak", None)
        baseline_fallbacks = copy.deepcopy(get_settings().get("config.fallback_models", None))

        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda url: FakeGitProvider(b'[config]\nmodel = "claude-3-5-sonnet"\n'),
        )
        git_utils.apply_repo_settings("https://git.example/projects/A/repos/a/pull-requests/1")
        assert "claude" in (get_settings().get("config.model_weak", "") or "").lower(), \
            "precondition: set_claude_model() should have rewritten model_weak"

        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda url: FakeGitProvider(b""),
        )
        git_utils.apply_repo_settings("https://git.example/projects/B/repos/b/pull-requests/1")

        assert get_settings().get("config.model_weak", None) == baseline_model_weak, \
            "config.model_weak leaked from the claude shorthand into the next repo"
        assert get_settings().get("config.fallback_models", None) == baseline_fallbacks, \
            "config.fallback_models leaked from the claude shorthand into the next repo"
