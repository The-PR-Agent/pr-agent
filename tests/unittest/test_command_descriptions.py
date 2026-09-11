from pathlib import Path

import pytest

from pr_agent.cli import set_parser
from pr_agent.command_descriptions import COMMAND_DESCRIPTIONS
from pr_agent.config_loader import get_settings
from pr_agent.servers.help import HelpMessage
from pr_agent.tools.pr_help_message import PRHelpMessage

REPO_ROOT = Path(__file__).resolve().parents[2]
CURRENT_DOCS_URL = "https://docs.pr-agent.ai"
RETIRED_DOCS_HOSTS = ("qodo-merge-docs.qodo.ai", "pr-agent-docs.codium.ai")


def test_cli_and_bot_help_use_canonical_command_descriptions():
    cli_usage = set_parser().format_usage()
    bot_help = HelpMessage.get_general_commands_text()

    for description in COMMAND_DESCRIPTIONS.values():
        assert description in cli_usage
        assert description in bot_help


@pytest.mark.parametrize(
    ("usage_guide", "command"),
    [
        (HelpMessage.get_describe_usage_guide, "describe"),
        (HelpMessage.get_review_usage_guide, "review"),
        (HelpMessage.get_improve_usage_guide, "improve"),
    ],
)
def test_tool_usage_guides_use_canonical_command_descriptions(usage_guide, command):
    assert COMMAND_DESCRIPTIONS[command] in usage_guide()


def test_runtime_help_uses_current_documentation_links():
    help_text = "\n".join(
        [
            HelpMessage.get_general_commands_text(),
            HelpMessage.get_general_bot_help_text(),
            HelpMessage.get_review_usage_guide(),
            HelpMessage.get_describe_usage_guide(),
            HelpMessage.get_ask_usage_guide(),
            HelpMessage.get_improve_usage_guide(),
            HelpMessage.get_help_docs_usage_guide(),
        ]
    )

    assert f"{CURRENT_DOCS_URL}/tools/review/#configuration-options" in help_text
    assert f"{CURRENT_DOCS_URL}/tools/ask/" in help_text
    assert not any(host in help_text for host in RETIRED_DOCS_HOSTS)


@pytest.mark.parametrize("command", COMMAND_DESCRIPTIONS)
def test_tool_docs_use_canonical_command_descriptions(command):
    tool_docs = REPO_ROOT / "docs" / "docs" / "tools" / f"{command}.md"
    assert COMMAND_DESCRIPTIONS[command] in tool_docs.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_pr_help_table_uses_canonical_command_descriptions():
    class FakeProvider:
        def __init__(self):
            self.comment = ""

        @staticmethod
        def is_supported(_feature):
            return True

        @staticmethod
        def supports_checkbox_commands():
            return False

        @staticmethod
        def supports_markdown_tables():
            return False

        def publish_comment(self, comment):
            self.comment = comment

    help_message = object.__new__(PRHelpMessage)
    help_message.git_provider = FakeProvider()
    help_message.question_str = ""

    settings = get_settings()
    publish_output = settings.config.publish_output
    settings.set("config.publish_output", True)
    try:
        await help_message.run()
    finally:
        settings.set("config.publish_output", publish_output)

    for description in COMMAND_DESCRIPTIONS.values():
        assert description in help_message.git_provider.comment
    assert "help_docs" not in help_message.git_provider.comment


def test_disabled_commands_are_not_advertised():
    cli_usage = set_parser().format_help()
    bot_help = HelpMessage.get_general_commands_text()

    assert "help_docs" not in cli_usage
    assert "help_docs" not in bot_help
    assert "reflect" not in cli_usage
