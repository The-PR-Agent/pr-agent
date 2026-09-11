from types import SimpleNamespace
from unittest.mock import MagicMock

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.tools.pr_reviewer import PRReviewer


def _set_issue_comments(provider, comments):
    provider.get_issue_comments.return_value = comments
    provider.get_issue_comments_newest_first.return_value = list(reversed(comments))


def _reviewer(provider):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.remaining_files_list = []
    provider.supports_review_finding_state.return_value = True
    provider.is_comment_authored_by_pr_agent.return_value = True
    provider.get_issue_comments_newest_first.side_effect = (
        lambda: list(reversed(provider.get_issue_comments()))
    )
    return reviewer


class _PersistentCommentProvider:
    publish_persistent_comment = GitProvider.publish_persistent_comment
    publish_persistent_comment_full = GitProvider.publish_persistent_comment_full
    supports_comment_editing = GitProvider.supports_comment_editing

    def __init__(self, comments):
        self.comments = comments
        self.edited = []
        self.published = []

    def get_issue_comments(self):
        return self.comments

    def get_issue_comments_newest_first(self):
        return list(reversed(self.comments))

    def get_latest_commit_url(self):
        return "commit-url"

    def get_comment_url(self, comment):
        return "comment-url"

    def edit_comment(self, comment, body):
        self.edited.append((comment, body))
        return True

    def publish_comment(self, body, **kwargs):
        self.published.append((body, kwargs))
        return "published"


def test_invalid_structured_finding_fails_closed(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.config, "publish_output", True)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_comment", True)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_finding_state", True, raising=False)

    provider = MagicMock()
    provider.is_supported.return_value = True
    _set_issue_comments(provider, [])
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
    _set_issue_comments(provider, [SimpleNamespace(body=header)])
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


def test_stateful_persistent_update_falls_back_when_enabled():
    header = "## PR Reviewer Guide 🔍"
    provider = MagicMock()
    _set_issue_comments(provider, [SimpleNamespace(body=header)])
    provider.get_latest_commit_url.return_value = "commit-url"
    provider.get_comment_url.return_value = "comment-url"
    provider.edit_comment.side_effect = RuntimeError("edit failed")
    provider.publish_comment.return_value = "fallback"

    result = GitProvider.publish_persistent_comment_full(
        provider,
        "new review",
        initial_header=header,
        update_header=False,
        final_update_message=False,
        fallback_on_error=True,
    )

    assert result == "fallback"
    provider.publish_comment.assert_called_once_with("new review")


def test_stateful_persistent_update_does_not_fallback_after_false_edit_failure():
    header = "## PR Reviewer Guide 🔍"
    provider = MagicMock()
    _set_issue_comments(provider, [SimpleNamespace(body=header)])
    provider.get_latest_commit_url.return_value = "commit-url"
    provider.get_comment_url.return_value = "comment-url"
    provider.edit_comment.return_value = False

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


def test_stateful_persistent_update_falls_back_after_false_edit_failure():
    header = "## PR Reviewer Guide 🔍"
    provider = MagicMock()
    _set_issue_comments(provider, [SimpleNamespace(body=header)])
    provider.get_latest_commit_url.return_value = "commit-url"
    provider.get_comment_url.return_value = "comment-url"
    provider.edit_comment.return_value = False
    provider.publish_comment.return_value = "fallback"

    result = GitProvider.publish_persistent_comment_full(
        provider,
        "new review",
        initial_header=header,
        update_header=False,
        final_update_message=False,
        fallback_on_error=True,
    )

    assert result == "fallback"
    provider.publish_comment.assert_called_once_with("new review")


def test_stateful_persistent_update_still_creates_first_comment():
    provider = MagicMock()
    _set_issue_comments(provider, [])
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


def test_stateful_mode_is_enabled_for_generic_persistent_publisher(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.config, "publish_output", True)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_comment", True)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_finding_state", True, raising=False)
    provider = MagicMock()
    provider.publish_persistent_comment = GitProvider.publish_persistent_comment.__get__(provider, type(provider))
    provider.is_supported.return_value = True
    reviewer = _reviewer(provider)
    assert reviewer._review_finding_state_enabled() is True


def test_base_persistent_comment_updates_matching_comment():
    header = "## PR Reviewer Guide 🔍"
    comment = SimpleNamespace(body=f"{header}\n\nold review")
    provider = _PersistentCommentProvider([comment])

    result = provider.publish_persistent_comment(
        f"{header}\n\nnew review",
        initial_header=header,
        update_header=False,
        final_update_message=False,
    )

    assert result is comment
    assert provider.edited == [(comment, f"{header}\n\nnew review")]
    assert provider.published == []


def test_base_persistent_comment_creates_comment_when_no_match_exists():
    provider = _PersistentCommentProvider([])

    result = provider.publish_persistent_comment(
        "new review",
        initial_header="## PR Reviewer Guide 🔍",
    )

    assert result == "published"
    assert provider.published == [("new review", {})]


def test_malformed_state_marker_is_replaced_without_duplicate_comment():
    header = "## PR Reviewer Guide 🔍"
    body = f"{header}\n\nold review\n\n<!-- pr-agent-review-state:v1\nnot-json\n-->"
    comment = SimpleNamespace(body=body)
    provider = MagicMock()
    _set_issue_comments(provider, [comment])
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
    _set_issue_comments(provider, [old, latest])
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


def test_persistent_update_accepts_dict_comments_and_uses_latest():
    header = "## PR Reviewer Guide 🔍"
    old = {"body": f"{header}\n\nold review", "id": 1}
    latest = {"body": f"{header}\n\nlatest review", "id": 2}
    provider = MagicMock()
    _set_issue_comments(provider, [old, latest])
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
    provider.publish_comment.assert_not_called()


def test_dict_comment_edit_failure_does_not_fallback():
    header = "## PR Reviewer Guide 🔍"
    comment = {"body": header, "id": 42}
    provider = MagicMock()
    _set_issue_comments(provider, [comment])
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
    provider.edit_comment.assert_called_once_with(comment, "new review")
    provider.publish_comment.assert_not_called()


class _NoEditProvider(_PersistentCommentProvider):
    """A provider that never implemented edit_comment, like gerrit and codecommit.

    The inherited base edit_comment is a no-op returning None, which
    publish_persistent_comment_full cannot tell apart from a successful edit.
    """

    edit_comment = GitProvider.edit_comment
    supports_comment_editing = GitProvider.supports_comment_editing


def test_provider_without_edit_comment_publishes_the_body_instead_of_losing_it():
    header = "## PR Reviewer Guide 🔍"
    comment = SimpleNamespace(body=f"{header}\n\nold review")
    provider = _NoEditProvider([comment])

    assert provider.supports_comment_editing() is False

    result = provider.publish_persistent_comment(
        f"{header}\n\nnew review",
        initial_header=header,
        update_header=False,
        final_update_message=False,
    )

    # The review body itself must reach the PR. Delegating to the full
    # implementation here would no-op the edit and publish only the
    # "updated to latest commit" status line, silently dropping the review.
    assert result == "published"
    assert provider.published == [(f"{header}\n\nnew review", {})]


def test_provider_with_edit_comment_reports_editing_support():
    provider = _PersistentCommentProvider([])
    assert GitProvider.supports_comment_editing(provider) is True
