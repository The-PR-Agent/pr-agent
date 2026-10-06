"""/review must not re-raise a key issue whose inline thread a human resolved as won't fix or by design.

The persistent state only knows findings PR-Agent resolved itself, so the dismissal comes from the inline
threads: a key-issue thread PR-Agent opened and someone else resolved reaches the model as "dismissed".
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from pr_agent.algo.comment_identity import PRReviewHeader
from pr_agent.algo.inline_comment_dedup import key_issue_body_with_markers
from pr_agent.algo.review_finding_state import (
    normalize_finding,
    reconcile_review_findings,
    render_previous_findings,
    serialize_review_state,
)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider
from pr_agent.tools.pr_reviewer import PRReviewer

_BOT_USER_ID = 7
_KEY_ISSUE_BODY = "**Possible Issue**\n\nThe lock is never released."
_SUGGESTION_BODY = "**Suggestion:** Rename this [best practice, importance: 5]\n<!-- pr-agent-dedup: aabbccddeeff -->"


def _key_issue_note(resolved_by=None, body=_KEY_ISSUE_BODY):
    note = {"author": {"id": _BOT_USER_ID}, "system": False, "resolvable": True,
            "body": key_issue_body_with_markers(body, "aabbccddeeff", "112233445566"),
            "position": {"position_type": "text", "new_path": "app.py", "old_path": "app.py", "new_line": 2}}
    if resolved_by is not None:
        note["resolved"] = True
        note["resolved_by"] = {"id": resolved_by}
    return note


def _discussion(notes, discussion_id):
    discussion = MagicMock()
    discussion.id = discussion_id
    discussion.attributes = {"id": discussion_id, "notes": notes}
    return discussion


def _gitlab_provider(discussions, own_user_id=_BOT_USER_ID):
    provider = GitLabProvider.__new__(GitLabProvider)
    provider._own_user_id = own_user_id
    provider.id_mr = 1
    provider.mr = MagicMock()
    provider.mr.discussions.list.return_value = discussions
    return provider


def _reviewer(provider):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider
    return reviewer


def test_gitlab_key_issue_resolved_by_a_human_is_dismissed_with_the_last_reply():
    human = _discussion([
        _key_issue_note(resolved_by=99),
        {"author": {"id": 99, "name": "Alice"}, "system": False, "body": "First thought."},
        {"author": {"id": 99, "name": "Alice"}, "system": False, "body": "By design: the caller releases it."},
        {"author": {"id": 99, "name": "Alice"}, "system": False, "body": "   "},
    ], "human")

    assert _reviewer(_gitlab_provider([human]))._load_dismissed_key_issues() == [
        {"path": "app.py", "body": _KEY_ISSUE_BODY, "line_start": 2, "line_end": 2,
         "reply": "By design: the caller releases it."},
    ]


def test_gitlab_ignores_bot_resolved_open_unverifiable_and_suggestion_threads():
    bot = _discussion([_key_issue_note(resolved_by=_BOT_USER_ID)], "bot")
    still_open = _discussion([_key_issue_note()], "open")
    suggestion = _discussion([dict(_key_issue_note(resolved_by=99), body=_SUGGESTION_BODY)], "suggestion")

    assert _reviewer(_gitlab_provider([bot, still_open, suggestion]))._load_dismissed_key_issues() == []
    human = _discussion([_key_issue_note(resolved_by=99)], "human")
    assert _reviewer(_gitlab_provider([human], own_user_id=None))._load_dismissed_key_issues() == []


def test_provider_without_thread_support_and_failing_provider_add_nothing():
    assert _reviewer(BitbucketProvider.__new__(BitbucketProvider))._load_dismissed_key_issues() == []
    failing = MagicMock()
    failing._iter_code_suggestion_threads.side_effect = RuntimeError("boom")
    assert _reviewer(failing)._load_dismissed_key_issues() == []


def test_without_dismissed_findings_the_block_is_unchanged():
    state = reconcile_review_findings(
        None, [{"path": "app.py", "body": _KEY_ISSUE_BODY, "line_start": 2, "line_end": 2}],
        allow_resolution=False, head_sha="head-1").state

    assert render_previous_findings(state, 10_000, ()) == render_previous_findings(state, 10_000)
    assert json.loads(render_previous_findings(state, 10_000))[0]["state"] == "active"


def test_dismissed_finding_replaces_the_active_one_and_sorts_before_resolved():
    first = reconcile_review_findings(
        None,
        [{"path": "app.py", "body": _KEY_ISSUE_BODY, "line_start": 2, "line_end": 2},
         {"path": "db.py", "body": "**Performance**\n\nOne query per row.", "line_start": 7, "line_end": 7},
         {"path": "api.py", "body": "**Security**\n\nThe token is logged.", "line_start": 4, "line_end": 4}],
        allow_resolution=False, head_sha="head-1").state
    state = reconcile_review_findings(
        first,
        [{"path": "app.py", "body": _KEY_ISSUE_BODY, "line_start": 2, "line_end": 2},
         {"path": "api.py", "body": "**Security**\n\nThe token is logged.", "line_start": 4, "line_end": 4}],
        allow_resolution=True, head_sha="head-2").state
    dismissed = [{"path": "app.py", "body": _KEY_ISSUE_BODY, "line_start": 3, "line_end": 3, "reply": "By design."}]

    context = json.loads(render_previous_findings(state, 10_000, dismissed))

    assert [(entry["state"], entry["relevant_file"]) for entry in context] == [
        ("active", "api.py"), ("dismissed", "app.py"), ("resolved", "db.py")]
    assert context[1] == {"state": "dismissed", "relevant_file": "app.py", "start_line": 3, "end_line": 3,
                          "issue_header": "Possible Issue", "issue_content": "The lock is never released.",
                          "reply": "By design."}


def test_tight_budget_keeps_active_before_dismissed_before_resolved():
    first = reconcile_review_findings(
        None,
        [{"path": "api.py", "body": "**Security**\n\nThe token is logged.", "line_start": 4, "line_end": 4},
         {"path": "db.py", "body": "**Performance**\n\nOne query per row.", "line_start": 7, "line_end": 7}],
        allow_resolution=False, head_sha="head-1").state
    state = reconcile_review_findings(
        first, [{"path": "api.py", "body": "**Security**\n\nThe token is logged.", "line_start": 4, "line_end": 4}],
        allow_resolution=True, head_sha="head-2").state
    dismissed = [{"path": "app.py", "body": _KEY_ISSUE_BODY, "line_start": 2, "line_end": 2}]
    full = render_previous_findings(state, 10_000, dismissed)

    def states(budget):
        return [entry["state"] for entry in json.loads(render_previous_findings(state, budget, dismissed))]

    assert states(len(full)) == ["active", "dismissed", "resolved"]
    assert states(len(full) - 1) == ["active", "dismissed"]
    active_only = len(render_previous_findings({"findings": [f for f in state["findings"]
                                                            if f["state"] == "ACTIVE"]}, 10_000))
    assert states(active_only) == ["active"]


def test_dismissed_finding_missing_from_the_state_is_rendered_within_the_budget():
    dismissed = [{"path": "app.py", "body": _KEY_ISSUE_BODY, "line_start": 2, "line_end": 2},
                 {"path": "big.py", "body": "**Bug**\n\n" + "x" * 500, "line_start": 1, "line_end": 1}]
    one_entry = len(render_previous_findings(None, 10_000, dismissed[:1]))

    context = json.loads(render_previous_findings(None, one_entry, dismissed))

    assert [entry["relevant_file"] for entry in context] == ["app.py"]
    assert "reply" not in context[0]
    assert render_previous_findings(None, 0, dismissed) == ""


def test_previous_findings_context_feeds_human_resolved_gitlab_threads(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.config, "publish_output", True)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_comment", True)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_finding_state", True)
    state = reconcile_review_findings(
        None, [{"path": "app.py", "body": _KEY_ISSUE_BODY, "line_start": 2, "line_end": 2}],
        allow_resolution=False, head_sha="head-1").state
    body = f"{PRReviewHeader.REGULAR.value} 🔍\n\nold review\n\n{serialize_review_state(state)}"
    provider = _gitlab_provider([_discussion([_key_issue_note(resolved_by=99)], "human")])
    provider.get_issue_comments_newest_first = lambda: [SimpleNamespace(body=body, author={"id": _BOT_USER_ID})]
    provider.is_supported = lambda capability: capability == "get_issue_comments"
    reviewer = _reviewer(provider)
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer._review_state_block_reason = None

    context = json.loads(reviewer._load_previous_findings_context())

    assert [entry["state"] for entry in context] == ["dismissed"]
    assert normalize_finding(reviewer._load_dismissed_key_issues()[0])["finding_id"] == \
        state["findings"][0]["finding_id"]


def test_dismissed_entry_too_large_for_the_budget_does_not_fall_back_to_the_active_finding():
    state = reconcile_review_findings(
        None, [{"path": "app.py", "body": _KEY_ISSUE_BODY, "line_start": 2, "line_end": 2}],
        allow_resolution=False, head_sha="head-1").state
    dismissed = [{"path": "app.py", "body": _KEY_ISSUE_BODY, "line_start": 2, "line_end": 2, "reply": "x" * 500}]

    assert render_previous_findings(state, len(render_previous_findings(state, 10_000)), dismissed) == ""
