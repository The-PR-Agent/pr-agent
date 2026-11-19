import re
from types import SimpleNamespace

import pytest
from jinja2 import Environment, meta, nodes
from jinja2.exceptions import SecurityError

from pr_agent.algo.prompt_fragments import render_diff_hunk_format, render_skills_prefix
from pr_agent.commands import pr_add_docs, pr_reviewer
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


class FakeGitProvider:
    pr = SimpleNamespace(title="A pull request")

    def __init__(self, description_files=None):
        self.description_files = description_files or []

    def get_languages(self):
        return {"Python": 1}

    def get_files(self):
        return ["example.py"]

    def get_pr_branch(self):
        return "feature"

    def get_commit_messages(self):
        return ""

    def get_pr_description(self, split_changes_walkthrough=False):
        if split_changes_walkthrough:
            return "Description", self.description_files
        return "Description"

    def get_num_of_files(self):
        return 1


@pytest.fixture
def restore_prompt_settings():
    snapshot = snapshot_settings([
        "config.enable_ai_metadata",
        "config.is_auto_command",
        "prompt_fragments.diff_hunk_format",
    ])
    yield
    restore_settings(snapshot)


def test_shared_diff_hunk_source_accepts_only_variant_flags():
    source = get_settings().prompt_fragments.diff_hunk_format
    environment = Environment()

    assert meta.find_undeclared_variables(environment.parse(source)) == {
        "include_line_numbers",
        "include_ai_metadata",
    }


@pytest.mark.parametrize("include_line_numbers", [False, True])
@pytest.mark.parametrize("include_ai_metadata", [False, True])
def test_render_diff_hunk_format_variants(include_line_numbers, include_ai_metadata):
    rendered = render_diff_hunk_format(
        include_line_numbers=include_line_numbers,
        include_ai_metadata=include_ai_metadata,
    )

    added_line = next(line for line in rendered.splitlines() if "+new code line2 added" in line)
    second_added_line = next(line for line in rendered.splitlines() if "+new code line5 added" in line)
    removed_line = next(line for line in rendered.splitlines() if "-old code line2 removed" in line)

    assert bool(re.match(r"^13 \+new code line2 added$", added_line)) is include_line_numbers
    assert bool(re.match(r"^22 \+new code line5 added$", second_added_line)) is include_line_numbers
    assert removed_line == "-old code line2 removed"
    assert ("line numbers" in rendered.lower()) is include_line_numbers
    assert ("### AI-generated changes summary:" in rendered) is include_ai_metadata
    assert ("may not be fully accurate" in rendered) is include_ai_metadata
    for delimiter in ("{{", "}}", "{%", "%}"):
        assert delimiter not in rendered


@pytest.mark.parametrize(
    "prompt_name,legacy_example",
    [
        ("pr_review_prompt", "11  unchanged code line0\n12  unchanged code line1"),
        ("pr_add_docs_prompt", "12  code line1 that remained unchanged in the PR"),
    ],
)
def test_affected_templates_delegate_to_shared_fragment_once(prompt_name, legacy_example):
    system_prompt = get_settings().get(prompt_name).system
    parsed = Environment().parse(system_prompt)
    fragment_references = [
        node for node in parsed.find_all(nodes.Name) if node.name == "diff_hunk_format"
    ]

    assert len(fragment_references) == 1
    assert legacy_example not in system_prompt


@pytest.mark.parametrize(
    ("prompt_name", "variable"),
    [
        ("pr_review_prompt", "diff"),
        ("pr_add_docs_prompt", "diff"),
        ("pr_questions_prompt", "diff"),
        ("pr_custom_labels_prompt", "diff"),
        ("pr_update_changelog_prompt", "diff"),
        ("pr_description_prompt", "diff"),
        ("pr_description_only_files_prompts", "diff"),
        ("pr_description_only_description_prompts", "diff"),
    ],
)
def test_diff_prompt_variables_do_not_use_content_trimming(prompt_name, variable):
    user_prompt = get_settings().get(prompt_name).user

    assert f"{{{{ {variable}|trim }}}}" not in user_prompt
    assert f"{{{{ {variable} }}}}" in user_prompt


def test_fragment_renderer_sandboxes_host_overrides(restore_prompt_settings):
    get_settings().set(
        "prompt_fragments.diff_hunk_format",
        "{{ cycler.__init__.__globals__.os.getcwd() }}",
    )

    with pytest.raises(SecurityError):
        render_diff_hunk_format(include_line_numbers=True, include_ai_metadata=False)


@pytest.mark.parametrize("include_ai_metadata", [False, True])
def test_command_initializers_supply_the_fragment_before_token_counting(
    monkeypatch,
    restore_prompt_settings,
    include_ai_metadata,
):
    description_files = [{"full_file_name": "example.py"}] if include_ai_metadata else []
    provider = FakeGitProvider(description_files=description_files)
    settings = get_settings()
    settings.set("config.enable_ai_metadata", include_ai_metadata)
    settings.set("config.is_auto_command", include_ai_metadata)

    monkeypatch.setattr(pr_reviewer, "get_git_provider_with_context", lambda _url: provider)
    monkeypatch.setattr(pr_reviewer, "get_main_pr_language", lambda _languages, _files: "Python")
    monkeypatch.setattr(pr_reviewer, "get_skills_context", lambda: "")
    monkeypatch.setattr(pr_reviewer, "build_repo_context", lambda _provider: "")
    monkeypatch.setattr(pr_reviewer, "add_ai_metadata_to_diff_files", lambda _provider, _files: None)

    monkeypatch.setattr(pr_add_docs, "get_git_provider_for_command", lambda _url: provider)
    monkeypatch.setattr(pr_add_docs, "get_main_pr_language", lambda _languages, _files: "Python")

    reviewer = pr_reviewer.PRReviewer("https://example/pr/1", ai_handler=lambda: SimpleNamespace())
    add_docs = pr_add_docs.PRAddDocs("https://example/pr/1", ai_handler=lambda: SimpleNamespace())

    assert reviewer.vars["diff_hunk_format"] == render_diff_hunk_format(
        include_line_numbers=True,
        include_ai_metadata=include_ai_metadata,
    )
    assert add_docs.vars["diff_hunk_format"] == render_diff_hunk_format(
        include_line_numbers=True,
        include_ai_metadata=False,
    )


_SKILLS_PREFIX_TEMPLATES = [
    "pr_review_prompt",
    "pr_description_prompt",
    "pr_description_only_description_prompts",
    "pr_description_only_files_prompts",
]


def _skills_prelude(prompt_name):
    """Everything between the start of the system template and its role line."""
    return get_settings().get(prompt_name).system.partition("You are PR-Reviewer")[0]


def test_review_commands_share_an_identical_skills_prefix():
    """/review, /describe and /improve open the system prompt with the same
    skills_context block, so prefix-based caches can reuse it across commands (#4007)."""
    rendered = [
        Environment(autoescape=True).from_string(_skills_prelude(name)).render({"skills_context": "sample skill"})
        for name in _SKILLS_PREFIX_TEMPLATES
    ]

    assert len(set(rendered)) == 1
    assert "\n\nOrganizational standards and review skills" in rendered[0]


@pytest.mark.parametrize("prompt_name", _SKILLS_PREFIX_TEMPLATES)
def test_review_commands_open_with_the_shared_skills_prefix(prompt_name):
    fragment = get_settings().prompt_fragments.skills_prefix
    system = get_settings().get(prompt_name).system

    assert system.startswith(fragment)
    rendered_prelude = Environment(autoescape=True).from_string(
        _skills_prelude(prompt_name)
    ).render(skills_context="sample skill")
    assert rendered_prelude == render_skills_prefix(skills_context="sample skill")


def test_render_skills_prefix_empty_without_context():
    assert render_skills_prefix(skills_context="") == ""


@pytest.mark.parametrize("prompt_name", _SKILLS_PREFIX_TEMPLATES)
def test_skills_prefix_is_the_only_skills_block_and_vanishes_when_empty(prompt_name):
    system = get_settings().get(prompt_name).system

    assert system.count("{%- if skills_context %}") == 1
    assert system.count("{{ skills_context }}") == 1
    assert Environment(autoescape=True).from_string(_skills_prelude(prompt_name)).render({"skills_context": ""}) == ""
