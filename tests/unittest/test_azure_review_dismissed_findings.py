"""Azure dismissal inference uses explicit status, verified roots, and the creation-default guard."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.comment_identity import PRReviewHeader
from pr_agent.algo.inline_comment_dedup import key_issue_body_with_markers
from pr_agent.algo.review_finding_state import reconcile_review_findings, serialize_review_state
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.azuredevops_provider import AzureDevopsProvider
from pr_agent.tools.pr_reviewer import PRReviewer

BOT = "11111111-1111-1111-1111-111111111111"
BODY = "**Possible Issue**\n\nThe lock is never released."


@pytest.fixture
def provider(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.azure_devops_server, "agent_identity", BOT)
    monkeypatch.setattr(settings.azure_devops, "default_comment_status", "closed")
    instance = AzureDevopsProvider.__new__(AzureDevopsProvider)
    instance._threads_cache = []
    instance.azure_devops_client = MagicMock()
    return instance


def thread(status="wontFix"):
    return {"id": 1, "status": status,
            "comments": [{"id": 1, "parentCommentId": 0, "author": {"id": BOT},
                          "content": key_issue_body_with_markers(BODY, "aabbccddeeff", "112233445566")}],
            "threadContext": {"filePath": "/app.py", "rightFileStart": {"line": 2}}}


def reviewer(provider):
    instance = PRReviewer.__new__(PRReviewer)
    instance.git_provider = provider
    instance.incremental = SimpleNamespace(is_incremental=False)
    instance._review_state_block_reason = None
    return instance


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("verify_author", [False, True])
def test_author_verification_gate_is_computed_once(provider, review, verify_author):
    provider._configured_stable_agent_identities = MagicMock(return_value={BOT} if verify_author else set())
    # Isolate the iterator's gate from the existing author verifier's own configuration reads.
    provider.is_comment_authored_by_pr_agent = MagicMock(return_value=True)
    items = [thread(), thread()]
    if not review:
        for item in items:
            item["comments"][0]["content"] = "```suggestion\nreplacement\n```"
    provider._threads_cache = items + [{"comments": []}]

    results = list(provider._iter_review_threads() if review else provider._iter_code_suggestion_threads())

    provider._configured_stable_agent_identities.assert_called_once_with()
    assert provider.is_comment_authored_by_pr_agent.call_count == (2 if verify_author else 0)
    assert len(results) == (0 if review and not verify_author else 2)


@pytest.mark.parametrize("status", ["wontFix", "byDesign"])
@pytest.mark.parametrize("default", ["closed", "wontFix", "byDesign"])
def test_explicit_dismissal_only_when_different_from_default(provider, monkeypatch, status, default):
    monkeypatch.setattr(get_settings().azure_devops, "default_comment_status", default)
    item = thread(status)
    item["comments"] += [
        {"content": "First reply"}, {"content": " By design: caller releases it. "}, {"content": "  "},
        {"content": "Deleted reason", "isDeleted": True},
        {"content": "Status updated", "commentType": "system"},
        {"content": "Working <!-- pr-agent-progress -->"}]
    provider._threads_cache = [item]
    findings = reviewer(provider)._load_dismissed_key_issues()
    if status == default:
        assert findings == []
    else:
        assert findings == [{"path": "/app.py", "body": BODY, "line_start": 2, "line_end": 2,
                             "reply": "By design: caller releases it."}]
    assert item["status"] == status
    provider.azure_devops_client.get_threads.assert_not_called()


@pytest.mark.parametrize("status", ["fixed", "closed", "active", "pending", "unknown", "resolved", None, 3, 5])
def test_other_or_unrecognized_statuses_are_not_dismissals(provider, status):
    provider._threads_cache = [thread(status)]
    assert reviewer(provider)._load_dismissed_key_issues() == []


@pytest.mark.parametrize("author", [None, {}, {"displayName": "PR-Agent"}, {"id": "someone-else"}])
def test_root_author_must_be_verified(provider, author):
    item = thread()
    item["comments"][0]["author"] = author
    provider._threads_cache = [item]
    assert reviewer(provider)._load_dismissed_key_issues() == []


@pytest.mark.parametrize("identity", ["", "PR-Agent"])
def test_identity_must_be_stable(provider, monkeypatch, identity):
    monkeypatch.setattr(get_settings().azure_devops_server, "agent_identity", identity)
    provider._threads_cache = [thread()]
    assert reviewer(provider)._load_dismissed_key_issues() == []


def test_only_root_marker_counts_and_improve_context_is_unchanged(provider):
    key_issue = thread()
    suggestion = thread()
    suggestion["id"] = 2
    suggestion["comments"][0]["content"] = "Try this\n```suggestion\nreplacement\n```"
    suggestion["comments"].append(key_issue["comments"][0])
    provider._threads_cache = [suggestion]
    before = provider.get_code_suggestion_thread_context()
    assert before
    assert reviewer(provider)._load_dismissed_key_issues() == []
    provider._threads_cache.append(key_issue)
    assert provider.get_code_suggestion_thread_context() == before
    assert len(reviewer(provider)._load_dismissed_key_issues()) == 1


def test_review_cleanup_does_not_change_suggestion_thread_parsing(provider):
    item = thread()
    item["isDeleted"] = True
    item["comments"][0]["content"] = "Try this\n```suggestion\nreplacement\n```"
    item["comments"] += [
        {"content": "System reply", "commentType": "system"},
        {"content": "Deleted reply", "isDeleted": True},
    ]
    provider._threads_cache = [item]

    # Preserve /improve's existing selection and reply handling, even for these edge cases.
    suggestions = list(provider._iter_code_suggestion_threads())
    assert len(suggestions) == 1
    assert suggestions[0].status == "wontFix"
    assert suggestions[0].text_replies() == [(None, "System reply"), (None, "Deleted reply")]
    assert reviewer(provider)._load_dismissed_key_issues() == []


@pytest.mark.parametrize("case", ["deleted_thread", "deleted_root", "reply_root", "no_comments",
                                  "no_path", "no_position", "zero_line", "bool_line", "reversed_range"])
def test_unusable_threads_are_ignored(provider, case):
    item = thread()
    if case == "deleted_thread":
        item["isDeleted"] = True
    elif case == "deleted_root":
        item["comments"][0]["isDeleted"] = True
    elif case == "reply_root":
        item["comments"][0]["parentCommentId"] = 1
    elif case == "no_comments":
        item["comments"] = []
    elif case == "no_path":
        item["threadContext"]["filePath"] = None
    elif case == "no_position":
        item["threadContext"].pop("rightFileStart")
    elif case in ("zero_line", "bool_line"):
        item["threadContext"]["rightFileStart"]["line"] = 0 if case == "zero_line" else True
    else:
        item["threadContext"]["rightFileEnd"] = {"line": 1}
    provider._threads_cache = [item]
    assert reviewer(provider)._load_dismissed_key_issues() == []


def test_sdk_objects_and_failed_thread_read(provider):
    item = thread()
    provider._threads_cache = [SimpleNamespace(
        id=1, status="byDesign", comments=[SimpleNamespace(**item["comments"][0])],
        thread_context=SimpleNamespace(file_path="/app.py", right_file_start=SimpleNamespace(line=2)))]
    assert len(reviewer(provider)._load_dismissed_key_issues()) == 1
    provider._get_threads = MagicMock(side_effect=RuntimeError("unavailable"))
    assert reviewer(provider)._load_dismissed_key_issues() == []


@pytest.mark.parametrize("default,expected", [("closed", "dismissed"), ("wontFix", "active")])
def test_previous_findings_context_uses_azure_cached_threads(provider, monkeypatch, default, expected):
    settings = get_settings()
    monkeypatch.setattr(settings.azure_devops, "default_comment_status", default)
    monkeypatch.setattr(settings.config, "publish_output", True)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_comment", True)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_finding_state", True)
    monkeypatch.setattr(settings.pr_reviewer, "max_previous_findings_chars", 8000)
    state = reconcile_review_findings(
        None, [{"path": "app.py", "body": BODY, "line_start": 2, "line_end": 2}],
        allow_resolution=False, head_sha="head-1").state
    review_body = f"{PRReviewHeader.REGULAR.value} 🔍\n\nold review\n\n{serialize_review_state(state)}"
    provider._threads_cache = [
        {"id": 2, "comments": [{"id": 1, "author": {"id": BOT}, "content": review_body}]}, thread()]
    instance = reviewer(provider)
    context = instance._load_previous_findings_context()
    assert len(context) <= 8000
    entries = json.loads(context)
    assert len(entries) == 1
    assert entries[0]["state"] == expected
    assert entries[0]["relevant_file"] == "app.py"
    assert entries[0]["issue_content"] == "The lock is never released."
    provider.azure_devops_client.get_threads.assert_not_called()
    instance.incremental.is_incremental = True
    assert instance._load_previous_findings_context() == ""
