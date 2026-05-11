from unittest.mock import Mock, patch

import pytest
from jinja2 import Environment, StrictUndefined

from pr_agent.algo import repo_context
from pr_agent.algo.repo_context import (
    TRUNCATION_MARKER,
    build_repo_context,
    render_instruction_files,
    render_instruction_files_with_line_budget,
)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.github_provider import GithubProvider


class FakeProvider:
    def __init__(self, files, pr_url=None):
        self.files = files
        self.pr_url = pr_url
        self.requested_paths = []

    def get_repo_file_content(self, file_path: str):
        self.requested_paths.append(file_path)
        return self.files.get(file_path)


class UnsupportedProvider:
    get_repo_file_content = GitProvider.get_repo_file_content


@pytest.fixture
def repo_context_settings():
    settings = get_settings()
    original_files = settings.config.get("repo_context_files", [])
    original_max_lines = settings.config.get("repo_context_max_lines", 500)
    original_warned_provider_classes = repo_context._unsupported_repo_context_provider_classes.copy()
    original_process_cache = repo_context._repo_context_process_cache.copy()

    yield settings

    settings.set("CONFIG.REPO_CONTEXT_FILES", original_files)
    settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", original_max_lines)
    repo_context._unsupported_repo_context_provider_classes = original_warned_provider_classes
    repo_context._repo_context_process_cache = original_process_cache


def test_build_repo_context_fetches_and_formats_configured_files(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md", "CONTRIBUTING.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    provider = FakeProvider({
        "AGENTS.md": "# Agent Guide\nUse focused tests.",
        "CONTRIBUTING.md": "Keep PRs small.",
    })

    context = build_repo_context(provider)

    assert context == (
        "You are being given instruction files. Follow them as project-specific guidance when reviewing code.\n"
        "<instruction_files>\n"
        '<file path="AGENTS.md" scope="repo-root">\n'
        "`````markdown\n"
        "# Agent Guide\n"
        "Use focused tests.\n"
        "`````\n"
        "</file>\n\n"
        '<file path="CONTRIBUTING.md" scope="repo-root">\n'
        "`````markdown\n"
        "Keep PRs small.\n"
        "`````\n"
        "</file>\n\n"
        "</instruction_files>"
    )
    assert provider.requested_paths == ["AGENTS.md", "CONTRIBUTING.md"]


def test_build_repo_context_reuses_provider_cache_for_same_config(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md", "CONTRIBUTING.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    provider = FakeProvider({
        "AGENTS.md": "Repo purpose",
        "CONTRIBUTING.md": "Keep PRs small.",
    })

    first_context = build_repo_context(provider)
    second_context = build_repo_context(provider)

    assert second_context == first_context
    assert provider.requested_paths == ["AGENTS.md", "CONTRIBUTING.md"]


def test_build_repo_context_reuses_process_cache_for_same_pr_url(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    first_provider = FakeProvider({"AGENTS.md": "Repo purpose"}, pr_url="https://example.com/org/repo/pull/1")
    second_provider = FakeProvider({"AGENTS.md": "Changed repo purpose"}, pr_url="https://example.com/org/repo/pull/1")

    first_context = build_repo_context(first_provider)
    second_context = build_repo_context(second_provider)

    assert second_context == first_context
    assert "Repo purpose" in second_context
    assert "Changed repo purpose" not in second_context
    assert first_provider.requested_paths == ["AGENTS.md"]
    assert second_provider.requested_paths == []


def test_build_repo_context_process_cache_invalidates_when_config_changes(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    first_provider = FakeProvider({
        "AGENTS.md": "Repo purpose",
        "CONTRIBUTING.md": "Keep PRs small.",
    }, pr_url="https://example.com/org/repo/pull/1")
    second_provider = FakeProvider({
        "AGENTS.md": "Repo purpose",
        "CONTRIBUTING.md": "Keep PRs small.",
    }, pr_url="https://example.com/org/repo/pull/1")

    first_context = build_repo_context(first_provider)
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["CONTRIBUTING.md"])
    second_context = build_repo_context(second_provider)

    assert "Repo purpose" in first_context
    assert "Keep PRs small." in second_context
    assert first_provider.requested_paths == ["AGENTS.md"]
    assert second_provider.requested_paths == ["CONTRIBUTING.md"]


def test_build_repo_context_does_not_cache_empty_context_after_fetch_error(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    provider = FakeProvider({"AGENTS.md": "Repo purpose"}, pr_url="https://example.com/org/repo/pull/1")
    provider.get_repo_file_content = Mock(side_effect=[Exception("temporary outage"), "Repo purpose"])

    first_context = build_repo_context(provider)
    second_context = build_repo_context(provider)

    assert first_context == ""
    assert "Repo purpose" in second_context
    assert provider.get_repo_file_content.call_count == 2


def test_build_repo_context_cache_invalidates_when_repo_context_files_change(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    provider = FakeProvider({
        "AGENTS.md": "Repo purpose",
        "CONTRIBUTING.md": "Keep PRs small.",
    })

    first_context = build_repo_context(provider)
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["CONTRIBUTING.md"])
    second_context = build_repo_context(provider)

    assert "Repo purpose" in first_context
    assert "Keep PRs small." in second_context
    assert provider.requested_paths == ["AGENTS.md", "CONTRIBUTING.md"]


def test_build_repo_context_cache_invalidates_when_line_budget_changes(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 9)
    provider = FakeProvider({"AGENTS.md": "one\ntwo\nthree"})

    truncated_context = build_repo_context(provider)
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    full_context = build_repo_context(provider)

    assert TRUNCATION_MARKER in truncated_context
    assert "one\ntwo\nthree" in full_context
    assert provider.requested_paths == ["AGENTS.md", "AGENTS.md"]


def test_render_instruction_files_escapes_path_and_derives_scope():
    context = render_instruction_files({
        'docs/Agent "Notes".md': "Use <literal> markers.\n",
    })

    assert context == (
        "You are being given instruction files. Follow them as project-specific guidance when reviewing code.\n"
        "<instruction_files>\n"
        '<file path="docs/Agent &quot;Notes&quot;.md" scope="docs">\n'
        "`````markdown\n"
        "Use <literal> markers.\n"
        "`````\n"
        "</file>\n\n"
        "</instruction_files>"
    )


def test_render_instruction_files_uses_longer_fence_when_content_contains_default_fence():
    context = render_instruction_files({
        "AGENTS.md": "Avoid closing this fence:\n`````",
    })

    assert context == (
        "You are being given instruction files. Follow them as project-specific guidance when reviewing code.\n"
        "<instruction_files>\n"
        '<file path="AGENTS.md" scope="repo-root">\n'
        "``````markdown\n"
        "Avoid closing this fence:\n"
        "`````\n"
        "``````\n"
        "</file>\n\n"
        "</instruction_files>"
    )


def test_render_instruction_files_with_line_budget_uses_longer_fence_for_conflicting_content():
    context = render_instruction_files_with_line_budget({
        "AGENTS.md": "Avoid closing this fence:\n`````",
    }, max_lines=500)

    assert context == (
        "You are being given instruction files. Follow them as project-specific guidance when reviewing code.\n"
        "<instruction_files>\n"
        '<file path="AGENTS.md" scope="repo-root">\n'
        "``````markdown\n"
        "Avoid closing this fence:\n"
        "`````\n"
        "``````\n"
        "</file>\n\n"
        "</instruction_files>"
    )


def test_build_repo_context_skips_invalid_missing_and_empty_files(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["", 7, "MISSING.md", "EMPTY.md", "AGENTS.md"])
    provider = FakeProvider({"EMPTY.md": "", "AGENTS.md": "Loaded context"})

    assert build_repo_context(provider) == (
        "You are being given instruction files. Follow them as project-specific guidance when reviewing code.\n"
        "<instruction_files>\n"
        '<file path="AGENTS.md" scope="repo-root">\n'
        "`````markdown\n"
        "Loaded context\n"
        "`````\n"
        "</file>\n\n"
        "</instruction_files>"
    )
    assert provider.requested_paths == ["MISSING.md", "EMPTY.md", "AGENTS.md"]


def test_build_repo_context_enforces_total_line_cap(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md", "CONTRIBUTING.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 4)
    provider = FakeProvider({
        "AGENTS.md": "one\ntwo\nthree",
        "CONTRIBUTING.md": "four\nfive",
    })

    context = build_repo_context(provider)

    assert context == (
        "You are being given instruction files. Follow them as project-specific guidance when reviewing code.\n"
        "<instruction_files>\n"
        "</instruction_files>"
    )
    assert len(context.splitlines()) <= 4


def test_render_instruction_files_with_line_budget_returns_empty_when_wrapper_exceeds_budget():
    context = render_instruction_files_with_line_budget({
        "AGENTS.md": "one",
    }, max_lines=2)

    assert context == ""


def test_build_repo_context_returns_empty_when_no_files_configured(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", [])

    assert build_repo_context(FakeProvider({"AGENTS.md": "repo purpose"})) == ""


def test_build_repo_context_treats_string_config_as_single_file(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", "AGENTS.md")
    provider = FakeProvider({"AGENTS.md": "repo purpose"})

    assert build_repo_context(provider) == (
        "You are being given instruction files. Follow them as project-specific guidance when reviewing code.\n"
        "<instruction_files>\n"
        '<file path="AGENTS.md" scope="repo-root">\n'
        "`````markdown\n"
        "repo purpose\n"
        "`````\n"
        "</file>\n\n"
        "</instruction_files>"
    )
    assert provider.requested_paths == ["AGENTS.md"]


def test_build_repo_context_skips_non_list_container(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", {"AGENTS.md": True})
    provider = FakeProvider({"AGENTS.md": "repo purpose"})

    assert build_repo_context(provider) == ""
    assert provider.requested_paths == []


def test_build_repo_context_warns_once_for_provider_without_repo_file_fetching(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    provider = UnsupportedProvider()

    with patch("pr_agent.algo.repo_context.get_logger") as mock_get_logger:
        context = build_repo_context(provider)
        second_context = build_repo_context(provider)

    assert context == ""
    assert second_context == ""
    mock_get_logger.return_value.warning.assert_called_once_with(
        "repo_context_files is configured, but UnsupportedProvider does not support repository file fetching; "
        "skipping repo context"
    )


def test_github_provider_decodes_repo_context_files_and_treats_failures_as_missing():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo_obj = Mock()
    provider.repo_obj.get_contents.return_value.decoded_content = b"repo context"

    assert provider.get_repo_file_content("AGENTS.md") == "repo context"

    provider.repo_obj.get_contents.side_effect = Exception("not found")

    assert provider.get_repo_file_content("MISSING.md") == ""


@pytest.mark.parametrize(
    "prompt_name,variables",
    [
        (
            "pr_review_prompt",
            {
                "extra_instructions": "",
                "repo_context": render_instruction_files({"AGENTS.md": "Repo purpose"}),
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
            },
        ),
        (
            "pr_description_prompt",
            {
                "extra_instructions": "",
                "repo_context": render_instruction_files({"AGENTS.md": "Repo purpose"}),
                "enable_custom_labels": False,
                "custom_labels_class": "",
                "enable_semantic_files_types": True,
                "include_file_summary_changes": True,
                "enable_pr_diagram": False,
            },
        ),
        (
            "pr_code_suggestions_prompt",
            {
                "extra_instructions": "",
                "repo_context": render_instruction_files({"AGENTS.md": "Repo purpose"}),
                "focus_only_on_problems": True,
                "num_code_suggestions": 3,
                "is_ai_metadata": False,
            },
        ),
        (
            "pr_code_suggestions_prompt_not_decoupled",
            {
                "extra_instructions": "",
                "repo_context": render_instruction_files({"AGENTS.md": "Repo purpose"}),
                "focus_only_on_problems": True,
                "num_code_suggestions": 3,
                "is_ai_metadata": False,
            },
        ),
    ],
)
def test_prompt_templates_render_configured_repo_context(prompt_name, variables):
    template = getattr(get_settings(), prompt_name).system

    rendered = Environment(undefined=StrictUndefined).from_string(template).render(variables)

    assert "Repository context:" in rendered
    assert '<file path="AGENTS.md" scope="repo-root">' in rendered
