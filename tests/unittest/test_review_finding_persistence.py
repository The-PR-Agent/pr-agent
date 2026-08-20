from types import SimpleNamespace
from unittest.mock import MagicMock

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.tools.pr_reviewer import PRReviewer


def _reviewer(provider):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.remaining_files_list = []
    return reviewer


def test_invalid_structured_finding_fails_closed(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.config, "publish_output", True)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_comment", True)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_finding_state", True, raising=False)

    provider = MagicMock()
    provider.is_supported.return_value = True
    provider.get_issue_comments.return_value = []
    reviewer = _reviewer(provider)

    reviewer._prepare_review_finding_state({
        "review": {"key_issues_to_review": {"not": "a list"}},
    })

    assert reviewer._review_state_blocked is True
    assert reviewer._review_state_result is None


def test_missing_structured_finding_collection_fails_closed():
    assert PRReviewer._review_findings_from_data({"review": {}}) is None


def test_stateful_persistent_update_does_not_fallback_after_edit_failure():
    header = "## PR Reviewer Guide 🔍"
    provider = MagicMock()
    provider.get_issue_comments.return_value = [SimpleNamespace(body=header)]
    provider.get_latest_commit_url.return_value = "commit-url"
    provider.get_comment_url.return_value = "comment-url"
    provider.edit_comment.side_effect = RuntimeError("edit failed")

    result = GitProvider.publish_persistent_comment_full(
        provider,
        "new review",
        initial_header=header,
        update_header=False,
        final_update_message=False,
        fallback_on_error=False,
    )

    assert result is None
    provider.publish_comment.assert_not_called()


def test_stateful_persistent_update_still_creates_first_comment():
    provider = MagicMock()
    provider.get_issue_comments.return_value = []
    provider.publish_comment.return_value = "created"

    result = GitProvider.publish_persistent_comment_full(
        provider,
        "new review",
        initial_header="## PR Reviewer Guide 🔍",
        final_update_message=False,
        fallback_on_error=False,
    )

    assert result == "created"
    provider.publish_comment.assert_called_once_with("new review")


def test_stateful_mode_is_disabled_for_generic_persistent_publisher(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.config, "publish_output", True)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_comment", True)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_finding_state", True, raising=False)
    provider = MagicMock()
    provider.publish_persistent_comment = GitProvider.publish_persistent_comment.__get__(provider, type(provider))
    provider.is_supported.return_value = True
    reviewer = _reviewer(provider)
    assert reviewer._review_finding_state_enabled() is False


def test_malformed_state_marker_is_replaced_without_duplicate_comment():
    header = "## PR Reviewer Guide 🔍"
    body = f"{header}\n\nold review\n\n<!-- pr-agent-review-state:v1\nnot-json\n-->"
    comment = SimpleNamespace(body=body)
    provider = MagicMock()
    provider.get_issue_comments.return_value = [comment]
    provider.get_latest_commit_url.return_value = "commit-url"
    provider.get_comment_url.return_value = "comment-url"

    result = GitProvider.publish_persistent_comment_full(
        provider,
        "new review",
        initial_header=header,
        update_header=False,
        final_update_message=False,
        fallback_on_error=False,
    )

    assert result is comment
    provider.edit_comment.assert_called_once_with(comment, "new review")
    provider.publish_comment.assert_not_called()


def test_persistent_update_uses_latest_matching_comment():
    header = "## PR Reviewer Guide 🔍"
    old = SimpleNamespace(body=f"{header}\n\nold review")
    latest = SimpleNamespace(body=f"{header}\n\nlatest review")
    provider = MagicMock()
    provider.get_issue_comments.return_value = [old, latest]
    provider.get_latest_commit_url.return_value = "commit-url"
    provider.get_comment_url.return_value = "comment-url"

    result = GitProvider.publish_persistent_comment_full(
        provider,
        "new review",
        initial_header=header,
        update_header=False,
        final_update_message=False,
        fallback_on_error=False,
    )

    assert result is latest
    provider.edit_comment.assert_called_once_with(latest, "new review")
