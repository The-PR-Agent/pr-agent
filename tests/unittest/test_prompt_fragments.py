import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jinja2 import Environment, meta, nodes
from jinja2.exceptions import SecurityError

from pr_agent.algo.prompt_fragments import render_diff_hunk_format
from pr_agent.config_loader import get_settings
from pr_agent.tools import pr_add_docs, pr_code_suggestions, pr_reviewer
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
        "pr_code_suggestions.decouple_hunks",
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
        ("pr_code_suggestions_prompt", "Important notes about the structured diff format above:"),
        (
            "pr_code_suggestions_reflect_prompt",
            "If no code was added or removed in a specific chunk, the corresponding section will be omitted.",
        ),
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


def test_fragment_renderer_sandboxes_host_overrides(restore_prompt_settings):
    get_settings().set(
        "prompt_fragments.diff_hunk_format",
        "{{ cycler.__init__.__globals__.os.getcwd() }}",
    )

    with pytest.raises(SecurityError):
        render_diff_hunk_format(include_line_numbers=True, include_ai_metadata=False)


@pytest.mark.parametrize("include_ai_metadata", [False, True])
def test_tool_initializers_supply_the_fragment_before_token_counting(
    monkeypatch,
    restore_prompt_settings,
    include_ai_metadata,
):
    description_files = [{"full_file_name": "example.py"}] if include_ai_metadata else []
    provider = FakeGitProvider(description_files=description_files)
    settings = get_settings()
    settings.set("config.enable_ai_metadata", include_ai_metadata)
    settings.set("config.is_auto_command", include_ai_metadata)
    settings.set("pr_code_suggestions.decouple_hunks", True)

    monkeypatch.setattr(pr_reviewer, "get_git_provider_with_context", lambda _url: provider)
    monkeypatch.setattr(pr_reviewer, "get_main_pr_language", lambda _languages, _files: "Python")
    monkeypatch.setattr(pr_reviewer, "get_skills_context", lambda: "")
    monkeypatch.setattr(pr_reviewer, "build_repo_context", lambda _provider: "")
    monkeypatch.setattr(pr_reviewer, "add_ai_metadata_to_diff_files", lambda _provider, _files: None)

    monkeypatch.setattr(pr_add_docs, "get_git_provider", lambda: lambda _url: provider)
    monkeypatch.setattr(pr_add_docs, "get_main_pr_language", lambda _languages, _files: "Python")

    monkeypatch.setattr(pr_code_suggestions, "get_git_provider_with_context", lambda _url: provider)
    monkeypatch.setattr(pr_code_suggestions, "get_main_pr_language", lambda _languages, _files: "Python")
    monkeypatch.setattr(pr_code_suggestions, "get_skills_context", lambda: "")
    monkeypatch.setattr(pr_code_suggestions, "build_repo_context", lambda _provider: "")
    monkeypatch.setattr(
        pr_code_suggestions,
        "add_ai_metadata_to_diff_files",
        lambda _provider, _files: None,
    )

    reviewer = pr_reviewer.PRReviewer("https://example/pr/1", ai_handler=lambda: SimpleNamespace())
    add_docs = pr_add_docs.PRAddDocs("https://example/pr/1", ai_handler=lambda: SimpleNamespace())
    suggestions = pr_code_suggestions.PRCodeSuggestions(
        "https://example/pr/1",
        ai_handler=lambda: SimpleNamespace(),
    )

    assert reviewer.vars["diff_hunk_format"] == render_diff_hunk_format(
        include_line_numbers=True,
        include_ai_metadata=include_ai_metadata,
    )
    assert add_docs.vars["diff_hunk_format"] == render_diff_hunk_format(
        include_line_numbers=True,
        include_ai_metadata=False,
    )
    assert suggestions.vars["diff_hunk_format"] == render_diff_hunk_format(
        include_line_numbers=False,
        include_ai_metadata=include_ai_metadata,
    )
    assert reviewer.token_handler.prompt_tokens > 0
    assert add_docs.token_handler.prompt_tokens > 0
    assert suggestions.token_handler.prompt_tokens > 0


def test_non_decoupled_suggestions_render_without_the_shared_fragment(monkeypatch, restore_prompt_settings):
    provider = FakeGitProvider()
    settings = get_settings()
    settings.set("config.enable_ai_metadata", False)
    settings.set("config.is_auto_command", False)
    settings.set("pr_code_suggestions.decouple_hunks", False)

    monkeypatch.setattr(pr_code_suggestions, "get_git_provider_with_context", lambda _url: provider)
    monkeypatch.setattr(pr_code_suggestions, "get_main_pr_language", lambda _languages, _files: "Python")
    monkeypatch.setattr(pr_code_suggestions, "get_skills_context", lambda: "")
    monkeypatch.setattr(pr_code_suggestions, "build_repo_context", lambda _provider: "")

    suggestions = pr_code_suggestions.PRCodeSuggestions(
        "https://example/pr/1",
        ai_handler=lambda: SimpleNamespace(),
    )

    assert suggestions.pr_code_suggestions_prompt_system == (
        settings.pr_code_suggestions_prompt_not_decoupled.system
    )
    assert suggestions.token_handler.prompt_tokens > 0


@pytest.mark.parametrize("include_ai_metadata", [False, True])
async def test_reflection_supplies_the_numbered_fragment(
    include_ai_metadata,
    restore_prompt_settings,
):
    settings = get_settings()
    settings.set("config.enable_ai_metadata", include_ai_metadata)
    ai_handler = SimpleNamespace(chat_completion=AsyncMock(return_value=("REFLECTION_OK", "stop")))
    tool = pr_code_suggestions.PRCodeSuggestions.__new__(pr_code_suggestions.PRCodeSuggestions)
    tool.ai_handler = ai_handler
    diff = "WITH-LINES {{ must_stay_literal_2959 }}"

    result = await tool.self_reflect_on_suggestions(
        [{"one_sentence_summary": "Keep the shared prompt accurate"}],
        diff,
        "test-model",
    )

    call = ai_handler.chat_completion.await_args.kwargs
    expected_fragment = render_diff_hunk_format(
        include_line_numbers=True,
        include_ai_metadata=include_ai_metadata,
    )
    assert result == "REFLECTION_OK"
    assert call["system"].count(expected_fragment) == 1
    assert diff in call["user"]


def test_non_decoupled_suggestions_prompt_remains_out_of_scope():
    section = get_settings().pr_code_suggestions_prompt_not_decoupled
    environment = Environment()

    system_variables = meta.find_undeclared_variables(environment.parse(section.system))
    user_variables = meta.find_undeclared_variables(environment.parse(section.user))

    assert "diff_hunk_format" not in system_variables
    assert "__new hunk__" not in section.system
    assert "__old hunk__" not in section.system
    assert "diff_no_line_numbers" in user_variables
