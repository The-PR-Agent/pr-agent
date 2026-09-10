"""The /describe help footer picks list markup from provider capabilities."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider
from pr_agent.tools import pr_description as pr_description_module
from pr_agent.tools.pr_description import PRDescription
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_SETTINGS = (
    "config.is_auto_command",
    "config.propagate_tool_errors",
    "config.publish_output",
    "pr_description.enable_help_comment",
    "pr_description.enable_help_text",
    "pr_description.enable_semantic_files_types",
    "pr_description.final_update_message",
    "pr_description.generate_ai_title",
    "pr_description.publish_description_as_comment",
    "pr_description.publish_labels",
    "pr_description.use_description_markers",
)

HTML_LIST_MARKER = "<li>Type <code>/help how to ...</code>"
PLAIN_BULLET_MARKER = "- Type <code>/help how to ...</code>"
PLAIN_BREAK_MARKER = "PR-Agent usage.<br>- Check out the"


class StubProvider:
    """A provider that is none of the concrete classes the tool used to branch on."""

    def __init__(self, html_lists: bool):
        self._html_lists = html_lists
        self.html_lists_queries = 0
        self.descriptions = []

    def is_supported(self, capability: str) -> bool:
        return capability == "gfm_markdown"

    def supports_inline_help_footer(self) -> bool:
        self.html_lists_queries += 1
        return self._html_lists

    def publish_description(self, title: str, body: str):
        self.descriptions.append((title, body))


@pytest.fixture
def configured_settings():
    snapshot = snapshot_settings(_TRACKED_SETTINGS)
    settings = get_settings()
    settings.config.is_auto_command = True
    settings.config.propagate_tool_errors = True
    settings.config.publish_output = True
    settings.pr_description.enable_help_comment = True
    settings.pr_description.enable_help_text = False
    settings.pr_description.enable_semantic_files_types = False
    settings.pr_description.final_update_message = False
    settings.pr_description.generate_ai_title = True
    settings.pr_description.publish_description_as_comment = False
    settings.pr_description.publish_labels = False
    settings.pr_description.use_description_markers = False
    yield
    restore_settings(snapshot)


async def render_description_help(provider, monkeypatch) -> str:
    description = PRDescription.__new__(PRDescription)
    description.pr_id = "1"
    description.git_provider = provider
    description.vars = {}
    description.prediction = "generated"
    description.data = None
    description.file_label_dict = None
    description._prepare_data = MagicMock()
    description._prepare_pr_answer = MagicMock(return_value=("AI title", "Description", "", []))

    monkeypatch.setattr(pr_description_module, "extract_and_cache_pr_tickets", AsyncMock())
    monkeypatch.setattr(pr_description_module, "retry_with_fallback_models", AsyncMock())

    await description.run()

    assert len(provider.descriptions) == 1
    return provider.descriptions[0][1]


@pytest.mark.parametrize(
    "html_lists, expected, unexpected",
    [
        (True, HTML_LIST_MARKER, PLAIN_BULLET_MARKER),
        (False, PLAIN_BULLET_MARKER, HTML_LIST_MARKER),
    ],
)
async def test_description_help_rendering_follows_html_list_capability(
    configured_settings, monkeypatch, html_lists, expected, unexpected
):
    provider = StubProvider(html_lists)

    body = await render_description_help(provider, monkeypatch)

    assert provider.html_lists_queries == 1
    assert expected in body
    assert unexpected not in body
    if not html_lists:
        assert PLAIN_BREAK_MARKER in body


def test_providers_declare_html_list_capability():
    assert GithubProvider.__new__(GithubProvider).supports_inline_help_footer() is True
    assert GitLabProvider.__new__(GitLabProvider).supports_inline_help_footer() is False
