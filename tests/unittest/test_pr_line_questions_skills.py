"""Regression tests for Agent Skills context in line-level /ask."""

from types import SimpleNamespace

from jinja2 import Environment, StrictUndefined

import pr_agent.tools.pr_line_questions as pr_line_questions
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


class _FakeProvider:
    def __init__(self):
        self.pr = SimpleNamespace(title="Skills context line question")

    def get_pr_branch(self):
        return "feature/skills"

    def supports_thread_resolution(self):
        return False

    def supports_threaded_pr_questions(self):
        return False


class _FakeAIHandler:
    main_pr_language = None


def _build_line_questions(monkeypatch):
    provider = _FakeProvider()
    monkeypatch.setattr(pr_line_questions, "get_git_provider", lambda: lambda _url: provider)
    monkeypatch.setattr(
        pr_line_questions, "get_skills_context", lambda: "### Skill: security-review", raising=False,
    )
    return pr_line_questions.PR_LineQuestions(
        "https://github.com/example/repo/pull/1",
        args=["Does this line follow the security policy?"],
        ai_handler=_FakeAIHandler,
    )


def test_line_ask_injects_skills_when_enabled(monkeypatch):
    settings = get_settings()
    saved = snapshot_settings(("skills.enabled",))
    try:
        settings.set("skills.enabled", True)
        tool = _build_line_questions(monkeypatch)
    finally:
        restore_settings(saved)

    assert tool.vars["skills_context"] == "### Skill: security-review"


def test_line_ask_does_not_load_skills_when_disabled(monkeypatch):
    settings = get_settings()
    saved = snapshot_settings(("skills.enabled",))
    calls = []
    try:
        settings.set("skills.enabled", False)
        # Construct directly: _build_line_questions would overwrite the spy with its own stub.
        provider = _FakeProvider()
        monkeypatch.setattr(pr_line_questions, "get_git_provider", lambda: lambda _url: provider)
        monkeypatch.setattr(
            pr_line_questions,
            "get_skills_context",
            lambda: calls.append("loaded") or "### Skill: should-not-load",
            raising=False,
        )
        tool = pr_line_questions.PR_LineQuestions(
            "https://github.com/example/repo/pull/1",
            args=["Does this line follow the security policy?"],
            ai_handler=_FakeAIHandler,
        )
    finally:
        restore_settings(saved)

    assert tool.vars["skills_context"] == ""
    assert calls == []


def test_line_ask_prompt_renders_skills_context():
    variables = {
        "skills_context": "### Skill: security-review\nRequire an authentication check.",
        "extra_instructions": "",
    }
    prompt = Environment(undefined=StrictUndefined, autoescape=True).from_string(
        get_settings().pr_line_questions_prompt.system
    ).render(variables)

    assert "Organizational standards and skills" in prompt
    assert "Require an authentication check." in prompt


def test_line_ask_prompt_omits_skills_block_when_empty():
    variables = {"skills_context": "", "extra_instructions": ""}
    prompt = Environment(undefined=StrictUndefined, autoescape=True).from_string(
        get_settings().pr_line_questions_prompt.system
    ).render(variables)

    assert "Organizational standards and skills" not in prompt
