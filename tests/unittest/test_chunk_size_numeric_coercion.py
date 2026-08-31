"""Coerce num_code_suggestions_per_chunk so /improve does not die in its constructor."""
import copy

import pytest

import pr_agent.tools.pr_code_suggestions as pcs
from pr_agent.config_loader import get_settings


class FakeAiHandler:
    main_pr_language = None


class FakePR:
    title = "a title"


class FakeGitProvider:
    pr = FakePR()

    def get_languages(self):
        return {"Python": 1}

    def get_files(self):
        return ["a.py"]

    def get_pr_branch(self):
        return "feature"

    def get_commit_messages(self):
        return ""

    def get_pr_description(self, split_changes_walkthrough=False):
        return "description", []


@pytest.fixture
def build_tool(monkeypatch):
    settings = get_settings(use_context=False)
    original = copy.deepcopy(settings.get("PR_CODE_SUGGESTIONS", None))
    monkeypatch.setattr(pcs, "get_git_provider_with_context", lambda url: FakeGitProvider())
    monkeypatch.setattr(pcs, "get_main_pr_language", lambda languages, files: "Python")
    monkeypatch.setattr(pcs, "get_skills_context", lambda: "")
    monkeypatch.setattr(pcs, "build_repo_context", lambda provider: "")
    monkeypatch.setattr(pcs, "TokenHandler", lambda *args, **kwargs: None)

    def build(value):
        settings.set("pr_code_suggestions.num_code_suggestions_per_chunk", value)
        return pcs.PRCodeSuggestions("https://github.com/o/r/pull/1", ai_handler=FakeAiHandler)

    yield build
    if original is not None:
        settings.set("PR_CODE_SUGGESTIONS", original)


def test_accept_a_quoted_chunk_size(build_tool):
    """Accept a quoted number, which is what TOML yields for a quoted value."""
    tool = build_tool("5")

    assert tool.vars["num_code_suggestions"] == 5


@pytest.mark.parametrize("value", ["abc", "", None, [1]])
def test_fall_back_for_an_unusable_chunk_size(build_tool, value):
    """Fall back to the default instead of raising out of PRCodeSuggestions.__init__."""
    tool = build_tool(value)

    assert tool.vars["num_code_suggestions"] == 3


def test_keep_a_numeric_chunk_size(build_tool):
    """Keep the existing behaviour for a genuinely numeric setting."""
    tool = build_tool(4)

    assert tool.vars["num_code_suggestions"] == 4
