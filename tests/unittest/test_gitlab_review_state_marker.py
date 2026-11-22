from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pr_agent.algo.review_finding_state import append_review_state, parse_review_state, reconcile_review_findings
from pr_agent.git_providers.gitlab_provider import GitLabProvider

ZWSP = "\u200b"


def _make_provider(max_chars=65000):
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.mr = MagicMock()
    provider.max_comment_chars = max_chars
    provider.temp_comments = []
    return provider


def _finding(body):
    return {"body": body, "path": "app.py", "line_start": 3, "line_end": 3}


def _reconcile(previous, bodies):
    return reconcile_review_findings(
        previous,
        [_finding(body) for body in bodies],
        allow_resolution=True,
        head_sha="head-1",
        timestamp="2026-01-01T00:00:00Z",
    )


def _review_with_state(body="Ask @octocat to release the lock."):
    state = _reconcile(None, [body]).state
    return append_review_state(f"## Review\n\n{body}", state), state


def _rerun_changes_state(published):
    previous = parse_review_state(published).state
    return _reconcile(previous, [finding["body"] for finding in previous["findings"]]).changed


def _settings(reply_to_trigger=False):
    settings = MagicMock()
    settings.get.side_effect = lambda key, default=None: {
        "GITLAB.REPLY_TO_TRIGGER_COMMENT": reply_to_trigger,
        "comment_id": "discussion-1" if reply_to_trigger else "",
    }.get(key, default)
    return settings


@pytest.mark.parametrize("reply_to_trigger", [False, True])
def test_publish_comment_keeps_the_review_state_marker_intact(reply_to_trigger):
    provider = _make_provider()
    review, state = _review_with_state()

    with patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=_settings(reply_to_trigger)):
        provider.publish_comment(review)

    create = provider.mr.discussions.get.return_value.notes.create if reply_to_trigger else provider.mr.notes.create
    published = create.call_args.args[0]["body"]
    assert published == review.replace("Ask @octocat", f"Ask @{ZWSP}octocat", 1)
    assert parse_review_state(published).state == state
    assert _rerun_changes_state(published) is False


def test_publish_comment_as_thread_keeps_the_review_state_marker_intact():
    provider = _make_provider()
    review, state = _review_with_state()

    with patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=_settings()):
        provider.publish_comment(review, as_thread=True)

    published = provider.mr.discussions.create.call_args.args[0]["body"]
    assert parse_review_state(published).state == state
    assert _rerun_changes_state(published) is False


def test_edit_comment_keeps_the_review_state_marker_intact():
    provider = _make_provider()
    review, state = _review_with_state()

    provider.edit_comment(SimpleNamespace(id=12), review)

    published = provider.mr.notes.update.call_args.args[1]["body"]
    assert published == review.replace("Ask @octocat", f"Ask @{ZWSP}octocat", 1)
    assert parse_review_state(published).state == state
    assert _rerun_changes_state(published) is False


def test_review_state_marker_survives_a_comment_grown_past_the_limit():
    review, state = _review_with_state("@a " * 400)
    provider = _make_provider(max_chars=len(review))

    provider.edit_comment(SimpleNamespace(id=12), review)

    published = provider.mr.notes.update.call_args.args[1]["body"]
    assert len(published) <= len(review)
    assert published.startswith(f"## Review\n\n@{ZWSP}a ")
    assert parse_review_state(published).state == state


def test_a_comment_without_a_marker_is_unchanged_by_the_split():
    provider = _make_provider()

    provider.edit_comment(SimpleNamespace(id=12), "Updated for @reviewer\n")

    provider.mr.notes.update.assert_called_once_with(12, {"body": f"Updated for @{ZWSP}reviewer\n"})
