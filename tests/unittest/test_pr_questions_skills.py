"""Regression tests for Agent Skills context in top-level /ask."""

from types import SimpleNamespace

from jinja2 import Environment, StrictUndefined

import pr_agent.tools.pr_questions as pr_questions
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


class _FakeProvider:
    def __init__(self):
        self.pr = SimpleNamespace(title="Skills context question")

    def get_languages(self):
        return ["Python"]

    def get_files(self):
        return ["src/example.py"]

    def get_pr_branch(self):
        return "feature/skills"

    def get_pr_description(self):
        return "Add a small change."

    def get_commit_messages(self):
        return "Add a small change"

    def supports_threaded_pr_questions(self):
        return False


class _FakeAIHandler:
    main_pr_language = None


def _build_pr_questions(monkeypatch):
    provider = _FakeProvider()
    monkeypatch.setattr(pr_questions, "get_git_provider", lambda: lambda _url: provider)
    monkeypatch.setattr(pr_questions, "get_main_pr_language", lambda _languages, _files: "Python")
    monkeypatch.setattr(pr_questions, "TokenHandler", lambda *_args: object())
    monkeypatch.setattr(pr_questions, "get_skills_context", lambda: "### Skill: security-review", raising=False)
    return pr_questions.PRQuestions(
        "https://github.com/example/repo/pull/1",
        args=["Does this follow the security policy?"],
        ai_handler=_FakeAIHandler,
    )


def test_top_level_ask_injects_skills_when_enabled(monkeypatch):
    settings = get_settings()
    saved = snapshot_settings(("skills.enabled",))
    try:
        settings.set("skills.enabled", True)
        tool = _build_pr_questions(monkeypatch)
    finally:
        restore_settings(saved)

    assert tool.vars["skills_context"] == "### Skill: security-review"


def test_top_level_ask_does_not_load_skills_when_disabled(monkeypatch):
    settings = get_settings()
    saved = snapshot_settings(("skills.enabled",))
    calls = []
    try:
        settings.set("skills.enabled", False)
        monkeypatch.setattr(
            pr_questions,
            "get_skills_context",
            lambda: calls.append("loaded") or "### Skill: should-not-load",
            raising=False,
        )
        tool = _build_pr_questions(monkeypatch)
    finally:
        restore_settings(saved)

    assert tool.vars["skills_context"] == ""
    assert calls == []


def test_top_level_ask_prompt_renders_skills_context():
    variables = {
        "skills_context": "### Skill: security-review\nRequire an authentication check.",
        "extra_instructions": "",
    }
    prompt = Environment(undefined=StrictUndefined, autoescape=True).from_string(
        get_settings().pr_questions_prompt.system
    ).render(variables)

    assert "Organizational standards and skills" in prompt
    assert "Require an authentication check." in prompt


def test_top_level_ask_prompt_omits_skills_block_when_empty():
    variables = {"skills_context": "", "extra_instructions": ""}
    prompt = Environment(undefined=StrictUndefined, autoescape=True).from_string(
        get_settings().pr_questions_prompt.system
    ).render(variables)

    assert "Organizational standards and skills" not in prompt
