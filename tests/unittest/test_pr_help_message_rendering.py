"""The /help walkthrough picks its rendering from provider capabilities, not provider classes."""

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider
from pr_agent.git_providers.bitbucket_server_provider import BitbucketServerProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider
from pr_agent.tools.pr_help_message import PRHelpMessage
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

INTERACTIVE_MARKER = "Trigger Interactively"
PLAIN_TABLE_MARKER = "| Tool  | Description |"
UNSUPPORTED_MARKER = "requires gfm markdown"


class StubProvider:
    """A provider that is none of the concrete classes the tool used to branch on."""

    def __init__(self, gfm_markdown: bool, markdown_tables: bool = False, checkbox_commands: bool = False):
        self._gfm_markdown = gfm_markdown
        self._markdown_tables = markdown_tables
        self._checkbox_commands = checkbox_commands
        self.published = []

    def is_supported(self, capability: str) -> bool:
        return self._gfm_markdown if capability == "gfm_markdown" else True

    def supports_markdown_tables(self) -> bool:
        return self._markdown_tables

    def supports_checkbox_commands(self) -> bool:
        return self._checkbox_commands

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        self.published.append(pr_comment)


async def run_walkthrough(provider) -> str:
    tool = PRHelpMessage.__new__(PRHelpMessage)
    tool.git_provider = provider
    tool.question_str = ""
    tool.return_as_string = False
    await tool.run()
    assert len(provider.published) == 1
    return provider.published[0]


@pytest.fixture
def published_output():
    snapshot = snapshot_settings(["config.publish_output", "config.disable_checkboxes"])
    get_settings().set("config.publish_output", True)
    yield
    restore_settings(snapshot)


@pytest.mark.parametrize(
    "capabilities, expected, unexpected",
    [
        # gfm markdown plus checkbox commands is the only combination that offers the checkboxes
        (dict(gfm_markdown=True, checkbox_commands=True), INTERACTIVE_MARKER, PLAIN_TABLE_MARKER),
        (dict(gfm_markdown=True), "<table>", INTERACTIVE_MARKER),
        # without gfm markdown, a provider that renders pipe tables gets the basic table
        (dict(gfm_markdown=False, markdown_tables=True), PLAIN_TABLE_MARKER, "<table>"),
        (dict(gfm_markdown=False), UNSUPPORTED_MARKER, "<table>"),
    ],
)
async def test_rendering_follows_capabilities(published_output, capabilities, expected, unexpected):
    comment = await run_walkthrough(StubProvider(**capabilities))
    assert expected in comment
    assert unexpected not in comment


async def test_checkboxes_stay_disabled_by_configuration(published_output):
    get_settings().set("config.disable_checkboxes", True)
    comment = await run_walkthrough(StubProvider(gfm_markdown=True, checkbox_commands=True))
    assert INTERACTIVE_MARKER not in comment
    assert "<table>" in comment


@pytest.mark.parametrize(
    "provider_class, checkbox_commands, markdown_tables",
    [
        (GithubProvider, True, False),
        (GitLabProvider, False, False),
        (BitbucketProvider, False, False),
        (BitbucketServerProvider, False, True),
    ],
)
def test_providers_declare_the_capabilities_the_tool_reads(provider_class, checkbox_commands, markdown_tables):
    provider = provider_class.__new__(provider_class)
    assert provider.supports_checkbox_commands() is checkbox_commands
    assert provider.supports_markdown_tables() is markdown_tables
