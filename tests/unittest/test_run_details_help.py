"""Run-details coverage for /help question answers and the static /help walkthrough."""

import pytest

from pr_agent.commands import pr_help_message
from pr_agent.commands.pr_help_message import PRHelpMessage
from pr_agent.config_loader import get_settings
from tests.unittest._run_details_command_helpers import (
    BACKUP,
    PRIMARY,
    TITLE,
    _model_stub,
    _provider,
    _published_bodies,
    _seed_stale_fallback_run,
    command_run_settings,
)


@pytest.fixture(autouse=True)
def _run_settings():
    with command_run_settings() as settings:
        yield settings


# ---------------------------------------------------------------- /help <question>

HELP_YAML = (
    "response: Enable automatic review in the repository settings.\n"
    "relevant_sections:\n"
    "  - file_name: /commands/review.md\n"
    "    relevant_section_header_string: Automatic review\n"
)


def _make_help(monkeypatch, response=HELP_YAML, question="How do I enable automatic review?",
               fail_primary=False, provider=None):
    monkeypatch.setattr(pr_help_message, "_load_help_docs_prompt", lambda: ("docs", {"commands/review.md"}))
    command = PRHelpMessage.__new__(PRHelpMessage)
    command.git_provider = provider or _provider()
    command.git_provider.pr_url = "https://example.com/org/repo/pull/1"
    command.question_str = question
    command.return_as_string = False
    command.vars = {"question": question, "snippets": ""}
    command._prepare_prediction = _model_stub(pr_help_message.load_yaml(response), fail_primary)
    return command


async def test_help_question_answer_appends_run_details_once(monkeypatch):
    command = _make_help(monkeypatch)

    await command.run()

    [body] = _published_bodies(command.git_provider)
    assert "### Answer:\nEnable automatic review" in body
    assert body.count(TITLE) == 1
    assert f"Model: {PRIMARY}" in body


async def test_help_question_reports_the_fallback_model(monkeypatch):
    command = _make_help(monkeypatch, fail_primary=True)

    await command.run()

    [body] = _published_bodies(command.git_provider)
    assert f"Model: {BACKUP} (fallback)" in body


@pytest.mark.parametrize(("response", "expected"), [
    ("Plain prose that is not YAML.", "Plain prose that is not YAML."),
    ("response: Not sure.\nrelevant_sections: []\n", "Could not find relevant information"),
])
async def test_help_fallback_answers_append_run_details(monkeypatch, response, expected):
    command = _make_help(monkeypatch, response=response)

    await command.run()

    [body] = _published_bodies(command.git_provider)
    assert expected in body
    assert body.count(TITLE) == 1


async def test_help_question_omits_run_details_when_disabled(monkeypatch):
    get_settings().set("config.output_run_details", False)
    command = _make_help(monkeypatch)

    await command.run()

    [body] = _published_bodies(command.git_provider)
    assert TITLE not in body


async def test_help_question_starts_a_fresh_run_context(monkeypatch):
    _seed_stale_fallback_run()
    command = _make_help(monkeypatch)

    await command.run()

    [body] = _published_bodies(command.git_provider)
    assert f"Model: {PRIMARY}" in body
    assert "(fallback)" not in body
    assert "stale-model" not in body


async def test_static_help_walkthrough_has_no_run_details(monkeypatch):
    _seed_stale_fallback_run()
    provider = _provider()
    provider.supports_checkbox_commands.return_value = True
    command = _make_help(monkeypatch, question="", provider=provider)

    await command.run()

    [body] = _published_bodies(provider)
    assert body.startswith("## PR Agent Walkthrough")
    assert TITLE not in body
