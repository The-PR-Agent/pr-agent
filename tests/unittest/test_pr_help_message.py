"""Unit tests for pr_agent/tools/pr_help_message.py (PRHelpMessage).

Covers the deterministic, model-free paths:
- module-level helpers ``extract_header`` and ``generate_bbdc_table``
- ``parse_args`` and ``format_markdown_header``
- ``run()`` output assembly for the no-question walkthrough comment
  (generic / GitHub / Bitbucket-server flavors) and the "question but no
  OpenAI key" early exit.

Instances are built with ``__new__`` so no git provider or AI handler is
constructed; ``run()`` only needs ``question_str`` and ``git_provider``.
"""

from unittest.mock import MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import BitbucketServerProvider, GithubProvider
from pr_agent.tools.pr_help_message import (
    PRHelpMessage,
    extract_header,
    generate_bbdc_table,
)
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


def _make_tool(question_str="", git_provider=None) -> PRHelpMessage:
    obj = PRHelpMessage.__new__(PRHelpMessage)
    obj.question_str = question_str
    obj.return_as_string = False
    obj.git_provider = git_provider if git_provider is not None else MagicMock()
    return obj


@pytest.fixture
def settings_guard():
    keys = ("config.publish_output", "openai.key")
    saved = snapshot_settings(keys)
    try:
        yield get_settings()
    finally:
        restore_settings(saved)


class TestExtractHeader:
    def test_extracts_first_header_before_snippet_content(self):
        snippet = (
            "Header 1: Installation Guide\n"
            "Header 2: Quick Start\n"
            "===Snippet content===\n"
            "Header 3: Ignored because after content marker\n"
        )
        assert extract_header(snippet) == "#installation-guide"

    def test_header_is_lowercased_and_hyphenated(self):
        snippet = "Header 1: Configuration Options Overview\n===Snippet content===\nbody"
        assert extract_header(snippet) == "#configuration-options-overview"

    def test_no_header_returns_empty_string(self):
        assert extract_header("just some text\n===Snippet content===\nbody") == ""

    def test_empty_snippet_returns_empty_string(self):
        assert extract_header("") == ""


class TestGenerateBbdcTable:
    def test_basic_table(self):
        table = generate_bbdc_table(["tool_a", "tool_b"], ["desc a", "desc b"])
        lines = table.splitlines()
        assert lines[0] == "| Tool  | Description | "
        assert lines[1] == "|--|--|"
        assert lines[2] == "| tool_a | desc a |"
        assert lines[3] == "| tool_b | desc b |"

    def test_mismatched_column_lengths_pad_with_empty(self):
        table = generate_bbdc_table(["tool_a"], ["desc a", "desc b"])
        assert "| tool_a | desc a |" in table
        assert "|  | desc b |" in table


class TestParseArgs:
    def test_joins_args(self):
        tool = _make_tool()
        assert tool.parse_args(["how", "do", "I", "review?"]) == "how do I review?"

    def test_empty_and_none_args(self):
        tool = _make_tool()
        assert tool.parse_args([]) == ""
        assert tool.parse_args(None) == ""


class TestFormatMarkdownHeader:
    @pytest.mark.parametrize(
        "header,expected",
        [
            ("# Getting Started", "getting-started"),
            ("## What's `new` (v2), huh?", "whats-new-v2-huh"),
            ("💎 Qodo Merge 💎", "qodo-merge"),
            ("Overview", "overview"),
            ("Wiki configuration file!", "wiki-configuration-file"),
        ],
    )
    def test_formats_headers(self, header, expected):
        tool = _make_tool()
        assert tool.format_markdown_header(header) == expected


class TestRunWalkthrough:
    async def test_generic_provider_gets_command_table(self, settings_guard):
        settings_guard.set("config.publish_output", True)
        provider = MagicMock()
        provider.is_supported.return_value = True
        tool = _make_tool(git_provider=provider)

        await tool.run()

        provider.publish_comment.assert_called_once()
        comment = provider.publish_comment.call_args[0][0]
        assert "## PR Agent Walkthrough" in comment
        assert "`/describe`" in comment  # generic table shows the Command column
        assert "`/generate_labels`" in comment
        assert "<!-- /describe -->" not in comment  # no GitHub checkboxes

    async def test_github_provider_gets_checkbox_table(self, settings_guard):
        settings_guard.set("config.publish_output", True)
        provider = GithubProvider.__new__(GithubProvider)
        published = []
        provider.publish_comment = published.append
        tool = _make_tool(git_provider=provider)

        await tool.run()

        assert len(published) == 1
        comment = published[0]
        assert "## PR Agent Walkthrough" in comment
        assert "Trigger Interactively" in comment
        assert "<!-- /describe -->" in comment  # interactive checkboxes
        assert "triggered automatically" in comment

    async def test_bitbucket_server_provider_gets_reduced_table(self, settings_guard):
        settings_guard.set("config.publish_output", True)
        provider = BitbucketServerProvider.__new__(BitbucketServerProvider)
        published = []
        provider.publish_comment = published.append
        tool = _make_tool(git_provider=provider)

        await tool.run()

        assert len(published) == 1
        comment = published[0]
        # BBDC only gets the 4 basic tools in a plain markdown table
        assert "| Tool  | Description |" in comment
        assert "DESCRIBE" in comment and "UPDATE CHANGELOG" in comment
        assert "HELP DOCS" not in comment
        assert "GENERATE CUSTOM LABELS" not in comment

    async def test_no_gfm_markdown_support_publishes_error(self, settings_guard):
        settings_guard.set("config.publish_output", True)
        provider = MagicMock()
        provider.is_supported.return_value = False
        tool = _make_tool(git_provider=provider)

        await tool.run()

        provider.publish_comment.assert_called_once()
        comment = provider.publish_comment.call_args[0][0]
        assert "requires gfm markdown" in comment

    async def test_no_publish_output_skips_comment(self, settings_guard):
        settings_guard.set("config.publish_output", False)
        provider = MagicMock()
        provider.is_supported.return_value = True
        tool = _make_tool(git_provider=provider)

        await tool.run()

        provider.publish_comment.assert_not_called()


class TestRunQuestionWithoutOpenAIKey:
    async def test_question_without_openai_key_publishes_notice(self, settings_guard):
        settings_guard.set("config.publish_output", True)
        settings_guard.set("openai.key", "")  # explicitly no key
        provider = MagicMock()
        tool = _make_tool(question_str="how do I configure the model?", git_provider=provider)

        await tool.run()

        provider.publish_comment.assert_called_once()
        comment = provider.publish_comment.call_args[0][0]
        assert "requires an OpenAI API key" in comment

    async def test_question_without_openai_key_no_publish_output(self, settings_guard):
        settings_guard.set("config.publish_output", False)
        settings_guard.set("openai.key", "")
        provider = MagicMock()
        tool = _make_tool(question_str="how do I configure the model?", git_provider=provider)

        await tool.run()

        provider.publish_comment.assert_not_called()
