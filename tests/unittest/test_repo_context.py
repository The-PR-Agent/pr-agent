from unittest.mock import Mock

from jinja2 import Environment, StrictUndefined

from pr_agent.algo.repo_context import build_repo_context
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.github_provider import GithubProvider


class FakeProvider:
    def __init__(self, files):
        self.files = files
        self.requested_paths = []

    def get_repo_file_content(self, file_path: str):
        self.requested_paths.append(file_path)
        return self.files.get(file_path)


def test_build_repo_context_returns_empty_when_no_files_configured():
    original_files = get_settings().config.get("repo_context_files", [])
    try:
        get_settings().set("CONFIG.REPO_CONTEXT_FILES", [])

        assert build_repo_context(FakeProvider({"AGENTS.md": "repo purpose"})) == ""
    finally:
        get_settings().set("CONFIG.REPO_CONTEXT_FILES", original_files)


def test_build_repo_context_fetches_and_formats_configured_files():
    original_files = get_settings().config.get("repo_context_files", [])
    original_max_lines = get_settings().config.get("repo_context_max_lines", 500)
    try:
        get_settings().set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md", "CONTRIBUTING.md"])
        get_settings().set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
        provider = FakeProvider({
            "AGENTS.md": "# Agent Guide\nUse focused tests.",
            "CONTRIBUTING.md": "Keep PRs small.",
        })

        context = build_repo_context(provider)

        assert context == (
            "## AGENTS.md\n"
            "# Agent Guide\n"
            "Use focused tests.\n\n"
            "## CONTRIBUTING.md\n"
            "Keep PRs small."
        )
        assert provider.requested_paths == ["AGENTS.md", "CONTRIBUTING.md"]
    finally:
        get_settings().set("CONFIG.REPO_CONTEXT_FILES", original_files)
        get_settings().set("CONFIG.REPO_CONTEXT_MAX_LINES", original_max_lines)


def test_build_repo_context_skips_missing_and_invalid_files():
    original_files = get_settings().config.get("repo_context_files", [])
    try:
        get_settings().set("CONFIG.REPO_CONTEXT_FILES", ["", 7, "MISSING.md", "AGENTS.md"])
        provider = FakeProvider({"AGENTS.md": "Loaded context"})

        assert build_repo_context(provider) == "## AGENTS.md\nLoaded context"
        assert provider.requested_paths == ["MISSING.md", "AGENTS.md"]
    finally:
        get_settings().set("CONFIG.REPO_CONTEXT_FILES", original_files)


def test_build_repo_context_enforces_total_line_cap():
    original_files = get_settings().config.get("repo_context_files", [])
    original_max_lines = get_settings().config.get("repo_context_max_lines", 500)
    try:
        get_settings().set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md", "CONTRIBUTING.md"])
        get_settings().set("CONFIG.REPO_CONTEXT_MAX_LINES", 4)
        provider = FakeProvider({
            "AGENTS.md": "one\ntwo\nthree",
            "CONTRIBUTING.md": "four\nfive",
        })

        context = build_repo_context(provider)

        assert context == "## AGENTS.md\none\ntwo\nthree"
    finally:
        get_settings().set("CONFIG.REPO_CONTEXT_FILES", original_files)
        get_settings().set("CONFIG.REPO_CONTEXT_MAX_LINES", original_max_lines)


def test_base_provider_repo_file_content_returns_empty():
    assert GitProvider.get_repo_file_content(None, "AGENTS.md") == ""


def test_github_provider_fetches_repo_file_content_from_default_branch():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo_obj = Mock()
    provider.repo_obj.get_contents.return_value.decoded_content = b"repo context"

    assert provider.get_repo_file_content("AGENTS.md") == "repo context"
    provider.repo_obj.get_contents.assert_called_once_with("AGENTS.md")


def test_reviewer_prompt_renders_repo_context_block():
    variables = {
        "extra_instructions": "",
        "repo_context": "## AGENTS.md\nRepo purpose",
        "require_can_be_split_review": False,
        "related_tickets": "",
        "require_estimate_contribution_time_cost": False,
        "require_score": False,
        "require_tests": True,
        "question_str": "",
        "require_security_review": True,
        "require_todo_scan": False,
        "require_estimate_effort_to_review": True,
        "num_max_findings": 3,
        "num_pr_files": 1,
        "is_ai_metadata": False,
    }

    rendered = Environment(undefined=StrictUndefined).from_string(
        get_settings().pr_review_prompt.system
    ).render(variables)

    assert "Repository context:" in rendered
    assert "## AGENTS.md" in rendered


def test_description_prompt_renders_repo_context_block():
    variables = {
        "extra_instructions": "",
        "repo_context": "## AGENTS.md\nRepo purpose",
        "enable_custom_labels": False,
        "custom_labels_class": "",
        "enable_semantic_files_types": True,
        "include_file_summary_changes": True,
        "enable_pr_diagram": False,
    }

    rendered = Environment(undefined=StrictUndefined).from_string(
        get_settings().pr_description_prompt.system
    ).render(variables)

    assert "Repository context:" in rendered
    assert "## AGENTS.md" in rendered


def test_code_suggestions_prompt_renders_repo_context_block():
    variables = {
        "extra_instructions": "",
        "repo_context": "## AGENTS.md\nRepo purpose",
        "focus_only_on_problems": True,
        "num_code_suggestions": 3,
        "is_ai_metadata": False,
    }

    rendered = Environment(undefined=StrictUndefined).from_string(
        get_settings().pr_code_suggestions_prompt.system
    ).render(variables)

    assert "Repository context:" in rendered
    assert "## AGENTS.md" in rendered
