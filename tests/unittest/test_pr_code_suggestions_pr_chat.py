from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.tools import pr_code_suggestions as pr_code_suggestions_module
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_SETTINGS = (
    "config.publish_output",
    "config.publish_output_progress",
    "config.is_auto_command",
    "pr_code_suggestions.commitable_code_suggestions",
    "pr_code_suggestions.demand_code_suggestions_self_review",
    "pr_code_suggestions.enable_chat_text",
    "pr_code_suggestions.enable_help_text",
    "pr_code_suggestions.persistent_comment",
    "pr_code_suggestions.dual_publishing_score_threshold",
)


class _CustomProvider(GitProvider):
    """Minimal external provider that inherits the default chat capability."""

    def is_supported(self, capability: str) -> bool:
        return False

    def get_files(self) -> list:
        return []

    def get_diff_files(self) -> list:
        return []

    def publish_description(self, pr_title: str, pr_body: str):
        pass

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        return False

    def get_languages(self):
        return {}

    def get_pr_branch(self):
        return ""

    def get_user_id(self):
        return ""

    def get_pr_description_full(self) -> str:
        return ""

    def get_repo_settings(self):
        return b""

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        pass

    def publish_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str, original_suggestion=None):
        pass

    def publish_inline_comments(self, comments: list[dict]):
        pass

    def remove_initial_comment(self):
        pass

    def remove_comment(self, comment):
        pass

    def get_issue_comments(self):
        return []

    def publish_labels(self, labels):
        pass

    def get_pr_labels(self, update=False):
        return []

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        return False

    def get_commit_messages(self) -> str:
        return ""



def test_git_provider_supports_pr_chat_defaults_to_false():
    assert _CustomProvider().supports_pr_chat() is False


def test_github_provider_supports_pr_chat_is_true():
    assert GithubProvider.supports_pr_chat(MagicMock()) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("supports_pr_chat", "expect_link"), [(True, True), (False, False)])
async def test_pr_chat_link_depends_on_provider_capability(monkeypatch, supports_pr_chat, expect_link):
    snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        provider.get_files.return_value = ["file.py"]
        provider.is_supported.side_effect = lambda capability: capability == "gfm_markdown"
        provider.supports_pr_chat.return_value = supports_pr_chat
        provider.should_publish_improve_as_thread.return_value = False

        tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
        tool.pr_url = "https://example.invalid/pr/1"
        tool.progress = "progress"
        tool.progress_response = None
        tool.git_provider = provider
        tool.incremental = SimpleNamespace(is_incremental=False)
        tool.generate_summarized_suggestions = MagicMock(return_value="## Suggestions")

        async def _fake_retry(*_args, **_kwargs):
            return {"code_suggestions": [{"label": "style", "suggestion_content": "clean up"}]}

        monkeypatch.setattr(pr_code_suggestions_module, "retry_with_fallback_models", _fake_retry)

        settings = get_settings()
        settings.config.publish_output = True
        settings.config.publish_output_progress = False
        settings.config.is_auto_command = True
        settings.pr_code_suggestions.commitable_code_suggestions = False
        settings.pr_code_suggestions.demand_code_suggestions_self_review = False
        settings.pr_code_suggestions.enable_chat_text = True
        settings.pr_code_suggestions.enable_help_text = False
        settings.pr_code_suggestions.persistent_comment = False
        settings.pr_code_suggestions.dual_publishing_score_threshold = 0

        await tool.run()

        published_body = provider.publish_comment.call_args.args[0]
        provider.supports_pr_chat.assert_called_once_with()
        assert ("start a [PR chat]" in published_body) is expect_link
    finally:
        restore_settings(snapshot)
