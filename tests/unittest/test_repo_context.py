from unittest.mock import Mock

import pytest
from jinja2 import Environment, StrictUndefined

from pr_agent.algo.repo_context import build_repo_context, render_instruction_files
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.github_provider import GithubProvider


class FakeProvider:
    def __init__(self, files):
        self.files = files
        self.requested_paths = []

    def get_repo_file_content(self, file_path: str):
        self.requested_paths.append(file_path)
        return self.files.get(file_path)


@pytest.fixture
def repo_context_settings():
    settings = get_settings()
    original_files = settings.config.get("repo_context_files", [])
    original_max_lines = settings.config.get("repo_context_max_lines", 500)

    yield settings

    settings.set("CONFIG.REPO_CONTEXT_FILES", original_files)
    settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", original_max_lines)


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

    assert build_repo_context(provider) == (
        "You are being given instruction files. Follow them as project-specific guidance when reviewing code.\n"
        "<instruction_files>\n"
        '<file path="AGENTS.md" scope="repo-root">\n'
        "`````markdown"
    )


def test_build_repo_context_returns_empty_when_no_files_configured(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", [])

    assert build_repo_context(FakeProvider({"AGENTS.md": "repo purpose"})) == ""


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
