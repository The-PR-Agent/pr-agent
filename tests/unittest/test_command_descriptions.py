import pytest

from pr_agent.cli import set_parser
from pr_agent.command_descriptions import COMMAND_DESCRIPTIONS
from pr_agent.config_loader import get_settings
from pr_agent.servers.help import HelpMessage
from pr_agent.tools.pr_help_message import PRHelpMessage


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


@pytest.mark.asyncio
async def test_pr_help_table_uses_canonical_command_descriptions():
    class FakeProvider:
        def __init__(self):
            self.comment = ""

        @staticmethod
        def is_supported(_feature):
            return True

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
