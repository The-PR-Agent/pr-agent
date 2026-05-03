import copy

import pytest

from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers import utils as git_utils


REPO_A_TOML = b"""
[pr_reviewer]
extra_instructions = "MARKER-FROM-REPO-A"

[pr_code_suggestions]
extra_instructions = "MARKER-FROM-REPO-A"
"""


class FakeGitProvider:
    """Minimal stand-in used only to feed ``get_repo_settings()`` into
    ``apply_repo_settings()``."""

    def __init__(self, repo_settings: bytes):
        self._repo_settings = repo_settings

    def get_repo_settings(self):
        return self._repo_settings

    # Touched only by handle_configurations_errors() on invalid TOML.
    def is_supported(self, feature):
        return False

    def publish_comment(self, body):
        pass

    def publish_persistent_comment(self, *args, **kwargs):
        pass


@pytest.fixture
def fresh_global_settings():
    """Snapshot and restore ``global_settings`` around each test so the
    module-level Dynaconf singleton doesn't carry state across tests."""
    snapshot = copy.deepcopy(global_settings.as_dict())
    yield
    for section in set(global_settings.as_dict().keys()) - set(snapshot.keys()):
        global_settings.unset(section)
    for section, contents in snapshot.items():
        global_settings.unset(section)
        global_settings.set(section, copy.deepcopy(contents), merge=False)


class TestApplyRepoSettings:
    def _extra_instructions(self, section: str) -> str:
        return get_settings().get(f"{section}.extra_instructions", "") or ""

    def test_repo_settings_from_toml_are_applied(
        self, fresh_global_settings, monkeypatch
    ):
        """Sanity: ``apply_repo_settings()`` loads ``extra_instructions`` from a
        repo's ``.pr_agent.toml``."""
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda url: FakeGitProvider(REPO_A_TOML),
        )
        git_utils.apply_repo_settings(
            "https://git.example/projects/A/repos/a/pull-requests/1"
        )

        assert "MARKER-FROM-REPO-A" in self._extra_instructions("pr_reviewer")
        assert "MARKER-FROM-REPO-A" in self._extra_instructions("pr_code_suggestions")

    def test_repo_without_toml_does_not_inherit_previous_repo_settings(
        self, fresh_global_settings, monkeypatch
    ):
        """Regression: after ``apply_repo_settings()`` loads a repo with a
        ``.pr_agent.toml``, a subsequent call for a repo WITHOUT
        ``.pr_agent.toml`` must not still carry the previous repo's
        ``extra_instructions``.

        Before the fix this failed — the per-section merge in
        ``apply_repo_settings()`` never cleared state from a prior repo, and
        when the next repo returned an empty settings blob, the whole load
        block was skipped and the prior state persisted.
        """
        # 1) first repo loads custom instructions
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda url: FakeGitProvider(REPO_A_TOML),
        )
        git_utils.apply_repo_settings(
            "https://git.example/projects/A/repos/a/pull-requests/1"
        )
        assert "MARKER-FROM-REPO-A" in self._extra_instructions("pr_reviewer"), (
            "precondition: repo A's marker should be present after its load"
        )

        # 2) second repo has no .pr_agent.toml (get_repo_settings() → b"")
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda url: FakeGitProvider(b""),
        )
        git_utils.apply_repo_settings(
            "https://git.example/projects/B/repos/b/pull-requests/1"
        )

        assert "MARKER-FROM-REPO-A" not in self._extra_instructions("pr_reviewer"), (
            "repo A's [pr_reviewer].extra_instructions leaked into repo B"
        )
        assert "MARKER-FROM-REPO-A" not in self._extra_instructions(
            "pr_code_suggestions"
        ), "repo A's [pr_code_suggestions].extra_instructions leaked into repo B"
