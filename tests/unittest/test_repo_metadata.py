"""
Tests for the add_repo_metadata feature in apply_repo_settings().

When config.add_repo_metadata is true, metadata files (AGENTS.md, QODO.md,
CLAUDE.md by default) are fetched from the PR's head branch and their contents
are appended to extra_instructions for every tool that supports it.
"""

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import utils as git_utils
from pr_agent.git_providers.utils import _is_safe_repo_file_path, apply_repo_settings


class FakeGitProvider:
    """Minimal git provider stub for testing repo metadata loading."""

    def __init__(self, repo_files=None):
        """
        Args:
            repo_files: dict mapping file names to their content strings.
                        Files not in the dict will return "" (not found).
        """
        self._repo_files = repo_files or {}

    def get_repo_settings(self):
        return ""

    def get_repo_file(self, file_path: str) -> str:
        return self._repo_files.get(file_path, "")


@pytest.fixture(autouse=True)
def _reset_settings():
    """Snapshot and restore all settings modified by tests to avoid cross-test leakage."""
    tool_sections = [
        "pr_reviewer", "pr_description", "pr_code_suggestions",
        "pr_add_docs", "pr_update_changelog", "pr_test", "pr_improve_component",
    ]
    original_extra = {}
    for section in tool_sections:
        section_obj = get_settings().get(section, None)
        if section_obj is not None:
            original_extra[section] = getattr(section_obj, 'extra_instructions', "")

    original_add_repo_metadata = get_settings().config.get("add_repo_metadata", False)
    original_file_list = get_settings().config.get("add_repo_metadata_file_list",
                                                    ["AGENTS.md", "QODO.md", "CLAUDE.md"])
    original_baseline = git_utils._extra_instructions_baseline.copy()

    yield

    for section, value in original_extra.items():
        get_settings().set(f"{section}.extra_instructions", value)
    get_settings().set("config.add_repo_metadata", original_add_repo_metadata)
    get_settings().set("config.add_repo_metadata_file_list", original_file_list)
    git_utils._extra_instructions_baseline.clear()
    git_utils._extra_instructions_baseline.update(original_baseline)


class TestRepoMetadata:
    def test_metadata_disabled_by_default(self, monkeypatch):
        """When add_repo_metadata is false, no metadata files are loaded."""
        provider = FakeGitProvider(repo_files={"AGENTS.md": "should not appear"})
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda pr_url: provider,
        )
        get_settings().set("config.add_repo_metadata", False)

        apply_repo_settings("https://example.com/pr/1")

        assert "should not appear" not in (get_settings().pr_reviewer.extra_instructions or "")

    def test_metadata_appended_to_extra_instructions(self, monkeypatch):
        """When enabled, metadata file contents are appended to extra_instructions."""
        provider = FakeGitProvider(repo_files={"AGENTS.md": "Review with care"})
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda pr_url: provider,
        )
        get_settings().set("config.add_repo_metadata", True)
        get_settings().set("config.add_repo_metadata_file_list", ["AGENTS.md"])

        apply_repo_settings("https://example.com/pr/1")

        assert "Review with care" in get_settings().pr_reviewer.extra_instructions
        assert "Review with care" in get_settings().pr_code_suggestions.extra_instructions

    def test_multiple_metadata_files_combined(self, monkeypatch):
        """Contents of multiple metadata files are joined together."""
        provider = FakeGitProvider(repo_files={
            "AGENTS.md": "Agent instructions",
            "CLAUDE.md": "Claude instructions",
        })
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda pr_url: provider,
        )
        get_settings().set("config.add_repo_metadata", True)
        get_settings().set("config.add_repo_metadata_file_list", ["AGENTS.md", "CLAUDE.md"])

        apply_repo_settings("https://example.com/pr/1")

        instructions = get_settings().pr_reviewer.extra_instructions
        assert "Agent instructions" in instructions
        assert "Claude instructions" in instructions

    def test_missing_metadata_files_skipped(self, monkeypatch):
        """Files that don't exist in the repo are silently skipped."""
        provider = FakeGitProvider(repo_files={"AGENTS.md": "Found this one"})
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda pr_url: provider,
        )
        get_settings().set("config.add_repo_metadata", True)
        get_settings().set("config.add_repo_metadata_file_list",
                           ["AGENTS.md", "NONEXISTENT.md"])

        apply_repo_settings("https://example.com/pr/1")

        instructions = get_settings().pr_reviewer.extra_instructions
        assert "Found this one" in instructions
        assert "NONEXISTENT" not in instructions

    def test_metadata_appended_to_existing_extra_instructions(self, monkeypatch):
        """Metadata is appended to (not replacing) any pre-existing extra_instructions."""
        provider = FakeGitProvider(repo_files={"AGENTS.md": "From agents file"})
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda pr_url: provider,
        )
        get_settings().set("config.add_repo_metadata", True)
        get_settings().set("config.add_repo_metadata_file_list", ["AGENTS.md"])
        get_settings().set("pr_reviewer.extra_instructions", "Existing instructions")

        apply_repo_settings("https://example.com/pr/1")

        instructions = get_settings().pr_reviewer.extra_instructions
        assert "Existing instructions" in instructions
        assert "From agents file" in instructions

    def test_custom_file_list(self, monkeypatch):
        """Users can specify a custom list of metadata files to search for."""
        provider = FakeGitProvider(repo_files={"CUSTOM.md": "Custom content"})
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda pr_url: provider,
        )
        get_settings().set("config.add_repo_metadata", True)
        get_settings().set("config.add_repo_metadata_file_list", ["CUSTOM.md"])

        apply_repo_settings("https://example.com/pr/1")

        assert "Custom content" in get_settings().pr_reviewer.extra_instructions


class TestRepoFilePathValidation:
    """Tests for _is_safe_repo_file_path to prevent directory traversal attacks."""

    @pytest.mark.parametrize("path", [
        "AGENTS.md",
        "CLAUDE.md",
        "docs/QODO.md",
        "some-file.txt",
    ])
    def test_safe_paths_accepted(self, path):
        assert _is_safe_repo_file_path(path) is True

    @pytest.mark.parametrize("path", [
        "../etc/passwd",
        "../../secrets.txt",
        "foo/../../etc/shadow",
        "/etc/passwd",
        "/absolute/path.md",
        "C:\\Windows\\system32\\config",
        "foo\\..\\bar",
        "",
        "   ",
        "\\leading-backslash",
        # Percent-encoded traversal attempts
        "%2e%2e/etc/passwd",
        "%2e%2e%2fetc%2fpasswd",
        "foo/%2e%2e/%2e%2e/etc/shadow",
        "%2F etc/passwd",
        # Double-encoded (still contains % after first decode)
        "%252e%252e/secrets",
    ])
    def test_unsafe_paths_rejected(self, path):
        assert _is_safe_repo_file_path(path) is False

    def test_traversal_path_not_loaded(self, monkeypatch):
        """A traversal path in add_repo_metadata_file_list must not reach get_repo_file."""
        calls = []

        class SpyGitProvider:
            def get_repo_settings(self):
                return ""
            def get_repo_file(self, file_path: str) -> str:
                calls.append(file_path)
                return "should not be used"

        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda pr_url: SpyGitProvider(),
        )
        get_settings().set("config.add_repo_metadata", True)
        get_settings().set("config.add_repo_metadata_file_list",
                           ["../secrets.txt", "/etc/passwd"])

        apply_repo_settings("https://example.com/pr/1")

        # Neither unsafe path should have been forwarded to the provider
        assert calls == []
        assert not (get_settings().pr_reviewer.extra_instructions or "")

    def test_repeated_calls_do_not_duplicate_metadata(self, monkeypatch):
        """Calling apply_repo_settings() twice must not append metadata twice."""
        provider = FakeGitProvider(repo_files={"AGENTS.md": "Do not duplicate me"})
        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            lambda pr_url: provider,
        )
        get_settings().set("config.add_repo_metadata", True)
        get_settings().set("config.add_repo_metadata_file_list", ["AGENTS.md"])

        apply_repo_settings("https://example.com/pr/1")
        apply_repo_settings("https://example.com/pr/1")

        instructions = get_settings().pr_reviewer.extra_instructions
        assert instructions.count("Do not duplicate me") == 1

    def test_cross_pr_metadata_does_not_accumulate(self, monkeypatch):
        """In non-context mode (CLI/polling), metadata from PR A must not leak into PR B."""
        # Simulate two different PRs with different metadata files
        repo_files_by_url = {
            "https://example.com/pr/1": {"AGENTS.md": "Instructions for repo A"},
            "https://example.com/pr/2": {"AGENTS.md": "Instructions for repo B"},
        }

        def fake_provider(pr_url):
            return FakeGitProvider(repo_files=repo_files_by_url[pr_url])

        monkeypatch.setattr(
            "pr_agent.git_providers.utils.get_git_provider_with_context",
            fake_provider,
        )
        get_settings().set("config.add_repo_metadata", True)
        get_settings().set("config.add_repo_metadata_file_list", ["AGENTS.md"])

        # Process PR A
        apply_repo_settings("https://example.com/pr/1")
        instructions = get_settings().pr_reviewer.extra_instructions
        assert "Instructions for repo A" in instructions

        # Process PR B — must replace, not accumulate
        apply_repo_settings("https://example.com/pr/2")
        instructions = get_settings().pr_reviewer.extra_instructions
        assert "Instructions for repo B" in instructions
        assert "Instructions for repo A" not in instructions
