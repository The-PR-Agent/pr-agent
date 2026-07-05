"""Render every prompt template in pr_agent/settings/ with StrictUndefined.

The tools render prompts with jinja2's StrictUndefined against the `self.vars`
dict they build in __init__, so a template that references a variable missing
from vars (or has a syntax error) only blows up at runtime — and
TokenHandler._get_system_user_tokens swallows the exception, hiding the bug
until the model receives an empty prompt. These tests catch both classes of
regression in CI:

1. A sweep over every ``*_prompt*`` section loaded into global_settings,
   rendering each template with auto-derived variables (truthy and falsy),
   which validates template syntax and any expression on a taken branch.
2. Construction of the real tool classes against a mock git provider,
   rendering the exact system/user prompt pair with the tool's real
   ``self.vars`` — the same render TokenHandler performs, minus the
   exception swallowing.
"""

from unittest.mock import MagicMock, patch

import pytest
from jinja2 import Environment, StrictUndefined, meta

from pr_agent.config_loader import global_settings


class _RenderStub:
    """Permissive stand-in for any template variable."""

    def __init__(self, truthy: bool = True):
        self._truthy = truthy

    def __getattr__(self, name):
        return _RenderStub(self._truthy)

    def __getitem__(self, key):
        return _RenderStub(self._truthy)

    def __call__(self, *args, **kwargs):
        return _RenderStub(self._truthy)

    def __iter__(self):
        return iter(())

    def __str__(self):
        return "stub"

    def __bool__(self):
        return self._truthy

    def __len__(self):
        return 1 if self._truthy else 0

    def __int__(self):
        return 1

    def __float__(self):
        return 1.0

    def __add__(self, other):
        return _RenderStub(self._truthy)

    __radd__ = __add__

    def __eq__(self, other):
        return self._truthy

    def __ne__(self, other):
        return not self._truthy

    def __lt__(self, other):
        return self._truthy

    __gt__ = __le__ = __ge__ = __lt__

    def __hash__(self):
        return 0


def _collect_prompt_templates():
    """Yield (section, key, template) for every jinja template in prompt sections."""
    params = []
    for section, value in global_settings.as_dict().items():
        if "prompt" not in section.lower() or not isinstance(value, dict):
            continue
        for key, template in value.items():
            if isinstance(template, str) and ("{{" in template or "{%" in template):
                params.append(pytest.param(template, id=f"{section.lower()}.{key.lower()}"))
    return params


PROMPT_TEMPLATES = _collect_prompt_templates()


def test_prompt_templates_were_discovered():
    # guard against the sweep silently going empty if settings loading changes
    assert len(PROMPT_TEMPLATES) > 15


@pytest.mark.parametrize("template", PROMPT_TEMPLATES)
@pytest.mark.parametrize("truthy", [True, False], ids=["truthy_vars", "falsy_vars"])
def test_prompt_template_renders_with_strict_undefined(template, truthy):
    environment = Environment(undefined=StrictUndefined)
    ast = environment.parse(template)  # raises TemplateSyntaxError on bad syntax
    variables = {name: _RenderStub(truthy) for name in meta.find_undeclared_variables(ast)}
    rendered = environment.from_string(template).render(variables)
    assert isinstance(rendered, str)


def _mock_git_provider():
    provider = MagicMock()
    provider.pr.title = "Test PR"
    provider.get_pr_branch.return_value = "feature-branch"
    provider.get_pr_description.side_effect = (
        lambda *args, **kwargs: ("Test description", []) if kwargs.get("split_changes_walkthrough") else "Test description"
    )
    provider.get_languages.return_value = {"Python": 80, "JavaScript": 20}
    provider.get_files.return_value = ["src/app.py", "src/app.js"]
    provider.get_num_of_files.return_value = 2
    provider.get_commit_messages.return_value = "fix: test commit"
    provider.get_pr_file_content.return_value = ""
    provider.get_user_description.return_value = "Test description"
    provider.get_issue_comments.return_value = []
    provider.is_supported.return_value = True
    return provider


def _render_with_tool_vars(tool, system_template: str, user_template: str):
    environment = Environment(undefined=StrictUndefined)
    environment.from_string(system_template).render(tool.vars)
    environment.from_string(user_template).render(tool.vars)


def _build_tool(module_path: str, factory_name: str, tool_factory):
    provider = _mock_git_provider()
    factory = (
        patch(f"{module_path}.{factory_name}", return_value=provider)
        if factory_name == "get_git_provider_with_context"
        else patch(f"{module_path}.{factory_name}", return_value=lambda *a, **k: provider)
    )
    with factory:
        return tool_factory()


def test_pr_reviewer_vars_render_review_prompt():
    from pr_agent.tools.pr_reviewer import PRReviewer

    tool = _build_tool(
        "pr_agent.tools.pr_reviewer", "get_git_provider_with_context",
        lambda: PRReviewer("https://github.com/org/repo/pull/1", ai_handler=MagicMock),
    )
    prompts = global_settings.pr_review_prompt
    _render_with_tool_vars(tool, prompts.system, prompts.user)


def test_pr_description_vars_render_description_prompt():
    from pr_agent.tools.pr_description import PRDescription

    tool = _build_tool(
        "pr_agent.tools.pr_description", "get_git_provider_with_context",
        lambda: PRDescription("https://github.com/org/repo/pull/1", ai_handler=MagicMock),
    )
    prompts = global_settings.pr_description_prompt
    _render_with_tool_vars(tool, prompts.system, prompts.user)


@pytest.mark.parametrize("decouple_hunks", [True, False], ids=["decoupled", "not_decoupled"])
def test_pr_code_suggestions_vars_render_prompts(decouple_hunks):
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

    original = global_settings.pr_code_suggestions.get("decouple_hunks", True)
    global_settings.set("pr_code_suggestions.decouple_hunks", decouple_hunks)
    try:
        tool = _build_tool(
            "pr_agent.tools.pr_code_suggestions", "get_git_provider_with_context",
            lambda: PRCodeSuggestions("https://github.com/org/repo/pull/1", ai_handler=MagicMock),
        )
    finally:
        global_settings.set("pr_code_suggestions.decouple_hunks", original)
    _render_with_tool_vars(tool, tool.pr_code_suggestions_prompt_system, tool.pr_code_suggestions_prompt_user)


def test_pr_questions_vars_render_questions_prompt():
    from pr_agent.tools.pr_questions import PRQuestions

    tool = _build_tool(
        "pr_agent.tools.pr_questions", "get_git_provider",
        lambda: PRQuestions("https://github.com/org/repo/pull/1", args=["What", "does", "this", "do?"],
                            ai_handler=MagicMock),
    )
    prompts = global_settings.pr_questions_prompt
    _render_with_tool_vars(tool, prompts.system, prompts.user)


def test_pr_update_changelog_vars_render_changelog_prompt():
    from pr_agent.tools.pr_update_changelog import PRUpdateChangelog

    tool = _build_tool(
        "pr_agent.tools.pr_update_changelog", "get_git_provider",
        lambda: PRUpdateChangelog("https://github.com/org/repo/pull/1", ai_handler=MagicMock),
    )
    prompts = global_settings.pr_update_changelog_prompt
    _render_with_tool_vars(tool, prompts.system, prompts.user)


def test_pr_add_docs_vars_render_add_docs_prompt():
    from pr_agent.tools.pr_add_docs import PRAddDocs

    tool = _build_tool(
        "pr_agent.tools.pr_add_docs", "get_git_provider",
        lambda: PRAddDocs("https://github.com/org/repo/pull/1", ai_handler=MagicMock),
    )
    prompts = global_settings.pr_add_docs_prompt
    _render_with_tool_vars(tool, prompts.system, prompts.user)
