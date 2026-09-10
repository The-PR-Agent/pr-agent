"""Question-mode /help must let a failed completion reach the fallback models.

Issue #3265: `PRHelpMessage._prepare_prediction` swallowed every exception and
returned an empty string, which `retry_with_fallback_models` reads as success.
The configured backup was therefore never attempted, and the empty response was
presented to the user as "could not find relevant information" -- a documentation
gap rather than the model failure it actually was.
"""

import pytest

from pr_agent.algo.token_handler import TokenHandler
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_help_message import PRHelpMessage
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

PRIMARY = "gpt-4o"
BACKUP = "gpt-4o-mini"

ANSWER_YAML = """\
response: |
  Set pr_reviewer.require_score_review to true.
relevant_sections:
- file_name: "docs/usage-guide/automations_and_usage.md"
  relevant_section_header_string: "Automatic tools"
"""

NO_INFORMATION_YAML = """\
response: |
  No relevant documentation.
relevant_sections: []
"""

NO_INFORMATION_MARKER = "Could not find relevant information to answer the question"


class RecordingProvider:
    pr_url = "https://github.com/owner/repo/pull/1"

    def __init__(self):
        self.published = []

    def is_supported(self, capability: str) -> bool:
        return True

    def supports_markdown_tables(self) -> bool:
        return True

    def supports_checkbox_commands(self) -> bool:
        return True

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        self.published.append(pr_comment)


class ScriptedAiHandler:
    """Answers per model id, so the test can see which models were reached."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.models_called = []

    async def chat_completion(self, model: str, **kwargs):
        self.models_called.append(model)
        outcome = self.responses[model]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, "stop"


@pytest.fixture
def help_settings():
    snapshot = snapshot_settings(
        [
            "config.publish_output",
            "config.model",
            "config.fallback_models",
            "config.temperature",
            "openai.key",
        ]
    )
    get_settings().set("config.publish_output", True)
    get_settings().set("config.model", PRIMARY)
    get_settings().set("config.fallback_models", [BACKUP])
    get_settings().set("config.temperature", 0.2)
    get_settings().set("openai.key", "test-key")
    yield
    restore_settings(snapshot)


def build_tool(ai_handler, provider):
    tool = PRHelpMessage.__new__(PRHelpMessage)
    tool.git_provider = provider
    tool.question_str = "How do I configure automatic reviews?"
    tool.return_as_string = False
    tool.ai_handler = ai_handler
    tool.vars = {"question": tool.question_str, "snippets": ""}
    tool.token_handler = TokenHandler(
        None,
        tool.vars,
        get_settings().pr_help_prompts.system,
        get_settings().pr_help_prompts.user,
    )
    return tool


async def test_failed_primary_completion_falls_back_to_the_backup_model(help_settings):
    provider = RecordingProvider()
    ai_handler = ScriptedAiHandler({PRIMARY: RuntimeError("primary unavailable"), BACKUP: ANSWER_YAML})
    tool = build_tool(ai_handler, provider)

    await tool.run()

    assert ai_handler.models_called == [PRIMARY, BACKUP]
    assert len(provider.published) == 1
    assert "Set pr_reviewer.require_score_review to true." in provider.published[0]
    assert NO_INFORMATION_MARKER not in provider.published[0]


async def test_completion_failure_is_not_reported_as_missing_documentation(help_settings):
    provider = RecordingProvider()
    ai_handler = ScriptedAiHandler(
        {PRIMARY: RuntimeError("primary unavailable"), BACKUP: RuntimeError("backup unavailable")}
    )
    tool = build_tool(ai_handler, provider)

    await tool.run()

    assert ai_handler.models_called == [PRIMARY, BACKUP]
    assert provider.published == []


async def test_a_genuine_no_information_answer_still_uses_the_no_information_message(help_settings):
    provider = RecordingProvider()
    ai_handler = ScriptedAiHandler({PRIMARY: NO_INFORMATION_YAML, BACKUP: ANSWER_YAML})
    tool = build_tool(ai_handler, provider)

    await tool.run()

    assert ai_handler.models_called == [PRIMARY]
    assert len(provider.published) == 1
    assert NO_INFORMATION_MARKER in provider.published[0]
