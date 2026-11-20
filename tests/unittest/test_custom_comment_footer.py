"""Verify the host-only custom footer appended to the bot's command results."""
import tomllib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.cli_args import CliArgs
from pr_agent.algo.review_finding_state import STATE_SCHEMA_VERSION, parse_review_state
from pr_agent.algo.run_output import append_run_details
from pr_agent.commands.pr_config import PRConfig
from pr_agent.commands.pr_description import PRDescription
from pr_agent.commands.pr_generate_labels import PRGenerateLabels
from pr_agent.commands.pr_help_docs import PRHelpDocs
from pr_agent.commands.pr_help_message import PRHelpMessage
from pr_agent.commands.pr_reviewer import PRReviewer
from pr_agent.commands.pr_similar_issue import PRSimilarIssue
from pr_agent.config_loader import get_settings
from pr_agent.config_security import is_repo_host_only_key
from pr_agent.git_providers.git_provider import GitProvider
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

FOOTER = "To disable PR-Agent add the label `no-pr-agent`."
RENDERED_FOOTER = f"\n\n___\n\n{FOOTER}\n"

_TRACKED_KEYS = (
    "config.custom_comment_footer",
    "config.output_run_details",
    "config.publish_output",
    "config.is_auto_command",
    "data",
    "pr_reviewer.enable_help_text",
    "pr_description.enable_semantic_files_types",
    "pr_description.publish_labels",
    "pr_description.use_description_markers",
    "pr_description.enable_help_text",
    "pr_description.enable_help_comment",
)


class _Provider:
    def is_supported(self, capability):
        return True


class _UnsupportedProvider(GitProvider):
    def is_supported(self, capability: str) -> bool:
        return False


async def _noop_async(*_args, **_kwargs):
    return None


@pytest.fixture
def footer_setting():
    snapshot = snapshot_settings(_TRACKED_KEYS)
    settings = get_settings()
    settings.set("config.custom_comment_footer", "")
    settings.set("config.output_run_details", False)
    settings.set("config.publish_output", True)
    settings.set("config.is_auto_command", False)
    try:
        yield settings
    finally:
        restore_settings(snapshot)


def test_footer_is_empty_by_default(footer_setting):
    """The shipped configuration adds no footer."""
    with open(get_settings().find_file("configuration.toml"), "rb") as config_file:
        assert tomllib.load(config_file)["config"]["custom_comment_footer"] == ""
    assert append_run_details("body", _Provider()) == "body"


def test_footer_is_appended_to_comment(footer_setting):
    """A configured footer is appended after a rule at the end of the comment."""
    footer_setting.set("config.custom_comment_footer", FOOTER)

    assert append_run_details("body", _Provider()) == "body" + RENDERED_FOOTER


def test_footer_comes_after_run_details(footer_setting):
    """The footer stays last when the run-details section is enabled."""
    footer_setting.set("config.custom_comment_footer", FOOTER)
    footer_setting.set("config.output_run_details", True)

    assert append_run_details("body", _Provider()).endswith(RENDERED_FOOTER)


def test_footer_can_be_left_to_the_caller(footer_setting):
    """include_footer=False leaves the footer out for callers that append it themselves."""
    footer_setting.set("config.custom_comment_footer", FOOTER)

    assert append_run_details("body", _Provider(), include_footer=False) == "body"


@pytest.mark.parametrize("value", [
    pytest.param("  \n ", id="blank"),
    pytest.param(123, id="env-parsed-int"),
])
def test_unusable_footer_is_ignored(footer_setting, value):
    """A blank or non-string footer leaves the comment unchanged."""
    footer_setting.set("config.custom_comment_footer", value)

    assert append_run_details("body", _Provider()) == "body"


def test_footer_is_host_only():
    """Repository settings and comment arguments cannot set the footer."""
    assert is_repo_host_only_key("config", "custom_comment_footer")
    assert not CliArgs.validate_user_args(["--config.custom_comment_footer=spoofed"])[0]


def _reviewer():
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.pr_url = "https://example/pr/1"
    reviewer.vars = {}
    reviewer.prediction = 'review:\n  estimated_effort_to_review_[1-5]: "2"\n  security_concerns: "No"\n'
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.remaining_files_list = []
    reviewer.git_provider = MagicMock()
    reviewer.git_provider.get_files.return_value = ["changed.py"]
    reviewer.git_provider.is_supported.side_effect = lambda cap: cap == "gfm_markdown"
    reviewer.git_provider.get_diff_files.return_value = []
    reviewer.git_provider.get_latest_commit_url.return_value = ""
    return reviewer


@pytest.mark.asyncio
async def test_review_ends_with_footer(footer_setting, monkeypatch):
    """The /review markdown ends with the configured footer."""
    footer_setting.set("config.custom_comment_footer", FOOTER)
    footer_setting.set("config.publish_output", False)
    footer_setting.pr_reviewer.enable_help_text = False
    monkeypatch.setattr("pr_agent.commands.pr_reviewer.extract_and_cache_pr_tickets", _noop_async)
    monkeypatch.setattr("pr_agent.commands.pr_reviewer.retry_with_fallback_models", _noop_async)

    await _reviewer().run()

    assert footer_setting.data["artifact"].endswith(RENDERED_FOOTER)


_STATE = {"schema_version": STATE_SCHEMA_VERSION, "findings": [], "last_run": {}}


@pytest.mark.parametrize("max_chars, footer_kept", [
    pytest.param(None, True, id="no-limit"),
    pytest.param(400, False, id="footer-does-not-fit"),
])
def test_review_state_outranks_footer(footer_setting, max_chars, footer_kept):
    """The footer precedes the review-state marker, and is dropped rather than the state when both do not fit."""
    footer_setting.set("config.custom_comment_footer", FOOTER * 8)
    reviewer = _reviewer()
    reviewer.git_provider.max_comment_chars = max_chars
    reviewer.git_provider.max_comment_length = None

    output = reviewer._append_review_state_with_footer("review body", _STATE)

    assert parse_review_state(output).valid
    assert (FOOTER * 8 in output) is footer_kept
    if max_chars is not None:
        assert len(output) <= reviewer._review_comment_max_chars()


@pytest.mark.asyncio
async def test_describe_body_ends_with_footer(footer_setting, monkeypatch):
    """The /describe body, which may become the PR description, carries the footer."""
    footer_setting.set("config.custom_comment_footer", FOOTER)
    footer_setting.set("config.publish_output", False)
    footer_setting.pr_description.enable_semantic_files_types = False
    footer_setting.pr_description.publish_labels = False
    footer_setting.pr_description.use_description_markers = False
    footer_setting.pr_description.enable_help_text = False
    footer_setting.pr_description.enable_help_comment = False
    description = PRDescription.__new__(PRDescription)
    description.pr_id = "1"
    description.vars = {}
    description.prediction = "prediction"
    description.file_label_dict = {}
    description.data = {}
    description.git_provider = MagicMock()
    description.git_provider.is_supported.side_effect = lambda cap: cap == "gfm_markdown"
    description._prepare_data = MagicMock()
    description._prepare_pr_answer = MagicMock(return_value=("AI title", "Base description body", ""))
    monkeypatch.setattr("pr_agent.commands.pr_description.extract_and_cache_pr_tickets", _noop_async)
    monkeypatch.setattr("pr_agent.commands.pr_description.retry_with_fallback_models", _noop_async)

    await description.run()

    assert footer_setting.data["artifact"].endswith(RENDERED_FOOTER)


@pytest.mark.asyncio
async def test_help_docs_answer_ends_with_footer(footer_setting, monkeypatch):
    """The published /help_docs answer carries the footer."""
    footer_setting.set("config.custom_comment_footer", FOOTER)
    command = PRHelpDocs.__new__(PRHelpDocs)
    command.question = "Where is this documented?"
    command.return_as_string = False
    command.vars = {"docs_url": "https://example.test", "question": command.question, "snippets": ""}
    command.git_provider = MagicMock()
    command.ai_handler = SimpleNamespace()
    command.token_handler = SimpleNamespace(count_tokens=lambda text, force_accurate=False: len(text))
    command._docs_input_token_limit = lambda: 10_000
    command._gen_filenames_to_contents_map_from_repo = lambda: {"README.md": "# Docs\nanswer here"}
    command._format_model_answer = lambda response, relevant_sections: response
    monkeypatch.setattr("pr_agent.commands.pr_help_docs.retry_with_fallback_models", _noop_async)
    monkeypatch.setattr("pr_agent.commands.pr_help_docs.load_yaml", lambda response: {"response": "answer"})

    await command.run()

    command.git_provider.publish_comment.assert_called_once_with("answer" + RENDERED_FOOTER)


@pytest.mark.asyncio
async def test_help_usage_comment_ends_with_footer(footer_setting):
    """The /help command list carries the footer."""
    footer_setting.set("config.custom_comment_footer", FOOTER)
    command = PRHelpMessage.__new__(PRHelpMessage)
    command.question_str = ""
    command.return_as_string = False
    command.git_provider = MagicMock()
    command.git_provider.is_supported.return_value = True

    await command.run()

    assert command.git_provider.publish_comment.call_args.args[0].endswith(RENDERED_FOOTER)


@pytest.mark.asyncio
async def test_labels_comment_ends_with_footer(footer_setting, monkeypatch):
    """The labels comment posted on providers without label support carries the footer."""
    footer_setting.set("config.custom_comment_footer", FOOTER)
    command = PRGenerateLabels.__new__(PRGenerateLabels)
    command.pr_id = "1"
    command.prediction = "prediction"
    command.git_provider = MagicMock()
    command.git_provider.is_supported.return_value = False
    command._prepare_labels = MagicMock(return_value=["bug fix"])
    command._write_json_output = MagicMock()
    monkeypatch.setattr("pr_agent.commands.pr_generate_labels.retry_with_fallback_models", _noop_async)

    await command.run()

    command.git_provider.publish_comment.assert_called_with(
        "## PR Labels:\nbug fix\n" + RENDERED_FOOTER, is_temporary=False
    )


@pytest.mark.asyncio
async def test_config_comment_ends_with_footer(footer_setting):
    """The /config listing carries the footer."""
    footer_setting.set("config.custom_comment_footer", FOOTER)
    command = PRConfig.__new__(PRConfig)
    command.git_provider = MagicMock()
    command._prepare_pr_configs = MagicMock(return_value="configs")

    await command.run()

    command.git_provider.publish_comment.assert_called_once_with("configs" + RENDERED_FOOTER)


@pytest.mark.asyncio
async def test_similar_issue_unsupported_message_ends_with_footer(footer_setting, monkeypatch):
    """The /similar_issue message on an unsupported provider carries the footer."""
    footer_setting.set("config.custom_comment_footer", FOOTER)
    provider = MagicMock()
    monkeypatch.setattr("pr_agent.commands.pr_similar_issue.get_git_provider", lambda: _UnsupportedProvider)
    monkeypatch.setattr("pr_agent.git_providers.get_git_provider_with_context", lambda _: provider)

    await PRSimilarIssue("https://gitlab.example.com/group/repo/-/merge_requests/1", None).run()

    assert provider.publish_comment.call_args.args[0].endswith(RENDERED_FOOTER)
