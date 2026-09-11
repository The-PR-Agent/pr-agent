"""Regression tests for provider-independent /help behavior."""

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider
from pr_agent.git_providers.bitbucket_server_provider import BitbucketServerProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider
from pr_agent.tools import pr_help_message as pr_help_message_module
from pr_agent.tools.pr_help_message import PRHelpMessage
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

INTERACTIVE_MARKER = "Trigger Interactively"
PLAIN_TABLE_MARKER = "| Tool  | Description |"
UNSUPPORTED_MARKER = "requires gfm markdown"
CURRENT_DOCS_URL = "https://docs.pr-agent.ai"
RETIRED_DOCS_HOSTS = ("qodo-merge-docs.qodo.ai", "pr-agent-docs.codium.ai")


class StubProvider:
    """A provider that is none of the concrete classes the tool used to branch on."""

    def __init__(self, gfm_markdown: bool, markdown_tables: bool = False, checkbox_commands: bool = False):
        self.pr_url = "https://example.com/org/repo/pull/1"
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


class StubAiHandler:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def chat_completion(self, *, model, temperature, system, user):
        self.calls.append({"model": model, "system": system, "user": user})
        if self.error:
            raise self.error
        return self.response, "stop"


class StubTokenHandler:
    @staticmethod
    def count_tokens(text):
        return len(text)


async def run_walkthrough(provider) -> str:
    tool = PRHelpMessage.__new__(PRHelpMessage)
    tool.git_provider = provider
    tool.question_str = ""
    tool.return_as_string = False
    await tool.run()
    assert len(provider.published) == 1
    return provider.published[0]


def build_question_tool(tmp_path, monkeypatch, handler):
    package_root = tmp_path / "package"
    docs_path = package_root / "docs" / "docs"
    review_doc = docs_path / "tools" / "review.md"
    review_doc.parent.mkdir(parents=True)
    review_doc.write_text("# Automatic review\n\nEnable automatic review in the repository settings.", encoding="utf-8")

    source_path = package_root / "pr_agent" / "tools" / "pr_help_message.py"
    monkeypatch.setattr(pr_help_message_module, "Path", lambda _: source_path)

    tool = PRHelpMessage.__new__(PRHelpMessage)
    tool.git_provider = StubProvider(gfm_markdown=True)
    tool.ai_handler = handler
    tool.question_str = "How do I configure automatic reviews?"
    tool.return_as_string = False
    tool.vars = {"question": tool.question_str, "snippets": ""}
    tool.token_handler = StubTokenHandler()
    return tool


@pytest.fixture
def published_output():
    snapshot = snapshot_settings(["config.publish_output", "config.disable_checkboxes"])
    get_settings().set("config.publish_output", True)
    yield
    restore_settings(snapshot)


@pytest.fixture
def non_openai_question_settings():
    keys = [
        "config.model",
        "config.fallback_models",
        "model_routing.enable",
        "openai.key",
        "openai.deployment_id",
    ]
    snapshot = snapshot_settings(keys)
    settings = get_settings()
    settings.set("config.model", "anthropic/claude-3-5-sonnet-20240620")
    settings.set("config.fallback_models", [])
    settings.set("model_routing.enable", False)
    settings.set("openai.key", None)
    settings.set("openai.deployment_id", None)
    yield
    restore_settings(snapshot)


async def test_question_reaches_configured_handler_without_openai_key(
    published_output, non_openai_question_settings, tmp_path, monkeypatch
):
    handler = StubAiHandler(
        response=(
            "response: Enable automatic review in the repository settings.\n"
            "relevant_sections:\n"
            "  - file_name: /tools/review.md\n"
            "    relevant_section_header_string: Automatic review\n"
        )
    )
    tool = build_question_tool(tmp_path, monkeypatch, handler)

    await tool.run()

    assert [call["model"] for call in handler.calls] == ["anthropic/claude-3-5-sonnet-20240620"]
    assert tool.question_str in handler.calls[0]["user"]
    assert "Enable automatic review in the repository settings." in handler.calls[0]["user"]
    assert "Enable automatic review in the repository settings." in tool.git_provider.published[0]
    assert "requires an OpenAI API key" not in tool.git_provider.published[0]


async def test_question_uses_configured_handler_error_path_without_openai_key(
    published_output, non_openai_question_settings, tmp_path, monkeypatch
):
    handler = StubAiHandler(error=RuntimeError("provider credentials missing"))
    tool = build_question_tool(tmp_path, monkeypatch, handler)

    await tool.run()

    assert [call["model"] for call in handler.calls] == ["anthropic/claude-3-5-sonnet-20240620"]
    assert tool.git_provider.published == []


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


@pytest.mark.parametrize("checkbox_commands", [False, True], ids=["without-checkboxes", "with-checkboxes"])
async def test_walkthrough_uses_current_documentation_site(published_output, checkbox_commands):
    comment = await run_walkthrough(StubProvider(gfm_markdown=True, checkbox_commands=checkbox_commands))

    assert (INTERACTIVE_MARKER in comment) is checkbox_commands
    assert f"{CURRENT_DOCS_URL}/tools/review/" in comment
    assert f"{CURRENT_DOCS_URL}/usage-guide/automations_and_usage/" in comment
    assert not any(host in comment for host in RETIRED_DOCS_HOSTS)


@pytest.mark.parametrize(
    ("file_name", "header", "expected"),
    [
        ("/index.md", "", f"{CURRENT_DOCS_URL}/"),
        ("/tools/review.md", "Automatic review", f"{CURRENT_DOCS_URL}/tools/review/#automatic-review"),
        ("/faq/index.md", "Frequently asked questions", f"{CURRENT_DOCS_URL}/faq/#frequently-asked-questions"),
    ],
)
def test_question_source_urls_use_canonical_documentation_paths(file_name, header, expected):
    tool = PRHelpMessage.__new__(PRHelpMessage)

    assert tool.format_docs_url(file_name, header) == expected


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
