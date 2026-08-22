from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.algo.review_finding_state import (
    parse_review_state,
    reconcile_review_findings,
    serialize_review_state,
)
from pr_agent.algo.utils import PRReviewHeader
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.tools.pr_reviewer import PRReviewer


def _reviewer(provider):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider
    reviewer.pr_url = "https://example.test/pull/1"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.remaining_files_list = []
    reviewer.prediction = "review: {}"
    reviewer.set_review_labels = MagicMock()
    reviewer._review_state_block_reason = None
    return reviewer


def _finding(body="The lock is never released."):
    return {"body": body, "path": "app.py", "line_start": 2, "line_end": 2}


def _settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.config, "publish_output", True)
    monkeypatch.setattr(settings.config, "is_auto_command", False, raising=False)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_comment", True)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_finding_state", True, raising=False)
    monkeypatch.setattr(settings.pr_reviewer, "inline_key_issues", False)
    monkeypatch.setattr(settings.pr_reviewer, "publish_output_no_suggestions", False)
    return settings


def test_prepare_review_reconciles_previous_state_and_renders_resolved_section(monkeypatch):
    settings = _settings(monkeypatch)
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    old_body = (
        f"{PRReviewHeader.REGULAR.value} 🔍\n\nold review\n\n"
        f"{serialize_review_state(previous)}"
    )
    provider = MagicMock()
    provider.get_issue_comments.return_value = [SimpleNamespace(body=old_body)]
    provider.get_diff_files.return_value = []
    provider.is_supported.side_effect = lambda capability: capability == "get_issue_comments"
    reviewer = _reviewer(provider)

    with (
        patch("pr_agent.tools.pr_reviewer.load_yaml", return_value={"review": {"key_issues_to_review": []}}),
        patch("pr_agent.tools.pr_reviewer.github_action_output"),
        patch("pr_agent.tools.pr_reviewer.convert_to_markdown_v2", return_value="No major issues detected"),
    ):
        review = reviewer._prepare_pr_review()

    assert "<summary>✅ Resolved findings</summary>" in review
    assert "The lock is never released." in review
    assert reviewer._review_state_result.resolved_ids == (previous["findings"][0]["finding_id"],)
    assert reviewer._review_state_result.state["last_run"]["complete"] is True
    assert settings.pr_reviewer.persistent_finding_state is True


def test_load_review_finding_state_uses_latest_matching_comment():
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    latest = reconcile_review_findings(
        previous,
        [],
        allow_resolution=True,
        timestamp="2026-01-01T00:01:00Z",
    ).state
    header = f"{PRReviewHeader.REGULAR.value} 🔍"
    old_body = f"{header}\n\nold review\n\n{serialize_review_state(previous)}"
    new_body = f"{header}\n\nnew review\n\n{serialize_review_state(latest)}"
    provider = MagicMock()
    provider.get_issue_comments.return_value = [
        SimpleNamespace(body=old_body),
        SimpleNamespace(body=new_body),
    ]
    reviewer = _reviewer(provider)

    parsed = reviewer._load_review_finding_state()
    assert parsed.valid is True
    assert parsed.state == latest


def test_load_review_finding_state_accepts_dict_comment():
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    header = f"{PRReviewHeader.REGULAR.value} 🔍"
    provider = MagicMock()
    provider.get_issue_comments.return_value = [
        {"body": f"{header}\n\nreview\n\n{serialize_review_state(previous)}"}
    ]
    reviewer = _reviewer(provider)

    parsed = reviewer._load_review_finding_state()

    assert parsed.valid is True
    assert parsed.state == previous


def test_load_review_finding_state_falls_back_to_older_valid_marker():
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    header = f"{PRReviewHeader.REGULAR.value} " + chr(0x1F50D)
    old_body = f"{header}\n\nold review\n\n{serialize_review_state(previous)}"
    malformed_body = (
        f"{header}\n\nlatest review\n\n"
        "<!-- pr-agent-review-state:v1\nnot-json\n-->"
    )
    provider = MagicMock()
    provider.get_issue_comments.return_value = [
        SimpleNamespace(body=old_body),
        SimpleNamespace(body=malformed_body),
    ]
    reviewer = _reviewer(provider)

    parsed = reviewer._load_review_finding_state()

    assert parsed.valid is True
    assert parsed.state == previous
    assert getattr(reviewer, "_review_state_blocked", False) is False


def test_load_review_finding_state_returns_none_when_all_markers_are_invalid():
    header = f"{PRReviewHeader.REGULAR.value} " + chr(0x1F50D)
    provider = MagicMock()
    provider.get_issue_comments.return_value = [
        SimpleNamespace(
            body=f"{header}\n\nold\n\n"
            "<!-- pr-agent-review-state:v1\nnot-json\n-->"
        ),
        SimpleNamespace(
            body=f"{header}\n\nlatest\n\n"
            "<!-- pr-agent-review-state:v2\n{}\n-->"
        ),
    ]
    reviewer = _reviewer(provider)

    parsed = reviewer._load_review_finding_state()

    assert parsed is None
    assert reviewer._review_state_blocked is True
    assert reviewer._review_state_block_reason == "invalid_marker"


def test_load_review_finding_state_skips_newer_comment_without_marker():
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    header = f"{PRReviewHeader.REGULAR.value} " + chr(0x1F50D)
    provider = MagicMock()
    provider.get_issue_comments.return_value = [
        SimpleNamespace(
            body=f"{header}\n\nreview\n\n{serialize_review_state(previous)}"
        ),
        SimpleNamespace(body="A regular review comment without state"),
    ]
    reviewer = _reviewer(provider)

    parsed = reviewer._load_review_finding_state()

    assert parsed.valid is True
    assert parsed.state == previous


def test_malformed_marker_self_heals_with_valid_marker(monkeypatch):
    settings = _settings(monkeypatch)
    monkeypatch.setattr(settings.pr_reviewer, "num_max_findings", 3)
    header = f"{PRReviewHeader.REGULAR.value} 🔍"
    comment = SimpleNamespace(
        body=f"{header}\n\nold review\n\n<!-- pr-agent-review-state:v1\nbad\n-->"
    )
    provider = MagicMock()
    provider.get_issue_comments.return_value = [comment]
    provider.get_diff_files.return_value = []
    provider.is_supported.side_effect = lambda capability: capability == "get_issue_comments"
    provider.max_comment_chars = 2000
    provider.get_latest_commit_url.return_value = "commit-url"
    provider.get_comment_url.return_value = "comment-url"

    def edit_comment(comment_obj, body):
        comment_obj.body = body

    provider.edit_comment.side_effect = edit_comment
    reviewer = _reviewer(provider)
    reviewer._review_finding_state_enabled = MagicMock(return_value=True)
    issue = {
        "relevant_file": "app.py",
        "issue_content": "current issue",
        "start_line": 2,
        "end_line": 2,
    }

    with (
        patch(
            "pr_agent.tools.pr_reviewer.load_yaml",
            return_value={"review": {"key_issues_to_review": [issue]}},
        ),
        patch("pr_agent.tools.pr_reviewer.github_action_output"),
        patch(
            "pr_agent.tools.pr_reviewer.convert_to_markdown_v2",
            return_value=f"{header}\n\nclean review",
        ),
    ):
        review = reviewer._prepare_pr_review()

    result = GitProvider.publish_persistent_comment_full(
        provider,
        review,
        initial_header=header,
        update_header=True,
        final_update_message=False,
        fallback_on_error=False,
    )

    assert result is comment
    assert "clean review" in comment.body
    assert comment.body.count("<!-- pr-agent-review-state:") == 1
    parsed = parse_review_state(comment.body)
    assert parsed.valid is True
    assert parsed.state == reviewer._review_state_result.state
    provider.publish_comment.assert_not_called()


def test_prepare_and_persisted_state_round_trip_preserves_marker_and_history(monkeypatch):
    settings = _settings(monkeypatch)
    monkeypatch.setattr(settings.pr_reviewer, "num_max_findings", 3)
    header = f"{PRReviewHeader.REGULAR.value} 🔍"
    previous = reconcile_review_findings(
        None,
        [
            {"body": "a-body", "path": "a.py", "line_start": 2, "line_end": 2},
            {"body": "b-body", "path": "b.py", "line_start": 3, "line_end": 3},
        ],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    comment = SimpleNamespace(
        body=f"{header}\n\nold review\n\n{serialize_review_state(previous)}"
    )
    provider = MagicMock()
    provider.get_issue_comments.return_value = [comment]
    provider.get_diff_files.return_value = []
    provider.is_supported.side_effect = lambda capability: capability == "get_issue_comments"
    provider.max_comment_chars = 1600
    provider.get_latest_commit_url.return_value = "commit-url"
    provider.get_comment_url.return_value = "comment-url"

    def edit_comment(comment_obj, body):
        comment_obj.body = GitProvider.limit_output_characters(
            provider, body, provider.max_comment_chars
        )

    provider.edit_comment.side_effect = edit_comment
    reviewer = _reviewer(provider)
    reviewer._review_finding_state_enabled = MagicMock(return_value=True)
    issue = {
        "relevant_file": "a.py",
        "issue_content": "a-body",
        "start_line": 2,
        "end_line": 2,
    }

    with (
        patch(
            "pr_agent.tools.pr_reviewer.load_yaml",
            return_value={"review": {"key_issues_to_review": [issue]}},
        ),
        patch("pr_agent.tools.pr_reviewer.github_action_output"),
        patch(
            "pr_agent.tools.pr_reviewer.convert_to_markdown_v2",
            return_value=f"{header}\n\n" + ("long human review " * 1000),
        ),
    ):
        review = reviewer._prepare_pr_review()

    result = GitProvider.publish_persistent_comment_full(
        provider,
        review,
        initial_header=header,
        update_header=True,
        final_update_message=False,
        fallback_on_error=False,
    )

    assert result is comment
    assert len(comment.body) <= provider.max_comment_chars
    assert comment.body.count("<!-- pr-agent-review-state:") == 1
    parsed = parse_review_state(comment.body)
    assert parsed.valid is True
    states = {finding["body"]: finding["state"] for finding in parsed.state["findings"]}
    assert states == {"a-body": "ACTIVE", "b-body": "RESOLVED"}
    assert "long human review" in comment.body
    provider.publish_comment.assert_not_called()


@pytest.mark.parametrize(
    ("incremental", "remaining_files", "prediction"),
    [
        pytest.param(True, [], "prediction", id="incremental"),
        pytest.param(False, ["large.py"], "prediction", id="token-excluded"),
        pytest.param(False, [], "", id="prediction-failed"),
    ],
)
def test_missing_findings_resolve_only_after_complete_successful_review(
    monkeypatch, incremental, remaining_files, prediction
):
    _settings(monkeypatch)
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    header = f"{PRReviewHeader.REGULAR.value} 🔍"
    provider = MagicMock()
    provider.get_issue_comments.return_value = [
        SimpleNamespace(body=f"{header}\n\nold review\n\n{serialize_review_state(previous)}")
    ]
    provider.is_supported.side_effect = lambda capability: capability == "get_issue_comments"
    reviewer = _reviewer(provider)
    reviewer.incremental.is_incremental = incremental
    reviewer.remaining_files_list = remaining_files
    reviewer.prediction = prediction
    # Exercise the reconciliation guard directly even though incremental stateful
    # publishing is disabled by the feature gate.
    reviewer._review_finding_state_enabled = MagicMock(return_value=True)

    reviewer._prepare_review_finding_state({"review": {"key_issues_to_review": []}})

    assert reviewer._review_state_result is not None
    finding = reviewer._review_state_result.state["findings"][0]
    assert finding["state"] == "ACTIVE"
    assert reviewer._review_state_result.state["last_run"]["complete"] is False


def test_finding_limit_prevents_resolution_of_missing_active_findings(monkeypatch):
    settings = _settings(monkeypatch)
    monkeypatch.setattr(settings.pr_reviewer, "num_max_findings", 3)
    previous_findings = [
        {"body": f"previous {label}", "path": f"{label}.py", "line_start": 2, "line_end": 2}
        for label in ("a", "b", "c")
    ]
    current_findings = [
        {"relevant_file": f"{label}.py", "issue_content": f"current {label}"}
        for label in ("d", "e", "f")
    ]
    previous = reconcile_review_findings(
        None,
        previous_findings,
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    header = f"{PRReviewHeader.REGULAR.value} 🔍"
    provider = MagicMock()
    provider.get_issue_comments.return_value = [
        SimpleNamespace(body=f"{header}\n\nold review\n\n{serialize_review_state(previous)}")
    ]
    provider.is_supported.side_effect = lambda capability: capability == "get_issue_comments"
    reviewer = _reviewer(provider)
    reviewer._review_finding_state_enabled = MagicMock(return_value=True)

    reviewer._prepare_review_finding_state({"review": {"key_issues_to_review": current_findings}})

    states = {finding["path"]: finding["state"] for finding in reviewer._review_state_result.state["findings"]}
    assert states == {
        "a.py": "ACTIVE",
        "b.py": "ACTIVE",
        "c.py": "ACTIVE",
        "d.py": "ACTIVE",
        "e.py": "ACTIVE",
        "f.py": "ACTIVE",
    }
    assert reviewer._review_state_result.resolved_ids == ()
    assert reviewer._review_state_result.state["last_run"]["complete"] is False


def test_review_comment_budget_reserves_persistent_update_header():
    provider = MagicMock()
    provider.max_comment_chars = 1000
    provider.get_latest_commit_url.return_value = "commit-url"
    reviewer = _reviewer(provider)

    update_suffix = "\n\n#### (Review updated until commit commit-url)\n"

    assert reviewer._review_comment_max_chars() == 1000 - len(update_suffix)


def test_invalid_review_data_blocks_lifecycle_without_using_old_state(monkeypatch):
    _settings(monkeypatch)
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    header = f"{PRReviewHeader.REGULAR.value} 🔍"
    provider = MagicMock()
    provider.get_issue_comments.return_value = [
        SimpleNamespace(body=f"{header}\n\nold review\n\n{serialize_review_state(previous)}")
    ]
    provider.is_supported.side_effect = lambda capability: capability == "get_issue_comments"
    reviewer = _reviewer(provider)

    reviewer._prepare_review_finding_state({"review": {"key_issues_to_review": {"invalid": True}}})

    assert reviewer._review_state_blocked is True
    assert reviewer._review_state_block_reason == "review_data"
    assert reviewer._review_state_result is None


def test_provider_read_failure_blocks_lifecycle_without_using_old_state(monkeypatch):
    _settings(monkeypatch)
    provider = MagicMock()
    provider.get_issue_comments.side_effect = RuntimeError("provider read failed")
    provider.is_supported.side_effect = lambda capability: capability == "get_issue_comments"
    reviewer = _reviewer(provider)

    reviewer._prepare_review_finding_state({"review": {"key_issues_to_review": []}})

    assert reviewer._review_state_blocked is True
    assert reviewer._review_state_block_reason == "read_error"
    assert reviewer._review_state_result is None


def test_missing_review_data_blocks_lifecycle_without_using_old_state(monkeypatch):
    _settings(monkeypatch)
    provider = MagicMock()
    provider.is_supported.side_effect = lambda capability: capability == "get_issue_comments"
    reviewer = _reviewer(provider)

    reviewer._prepare_review_finding_state({})

    assert reviewer._review_state_blocked is True
    assert reviewer._review_state_block_reason == "review_data"
    assert reviewer._review_state_result is None


def test_invalid_marker_does_not_mask_invalid_review_data(monkeypatch):
    _settings(monkeypatch)
    header = f"{PRReviewHeader.REGULAR.value} 🔍"
    provider = MagicMock()
    provider.get_issue_comments.return_value = [
        SimpleNamespace(body=f"{header}\n\nold review\n\n<!-- pr-agent-review-state:v1\nbad\n-->")
    ]
    provider.is_supported.side_effect = lambda capability: capability == "get_issue_comments"
    reviewer = _reviewer(provider)

    reviewer._prepare_review_finding_state({"review": {"key_issues_to_review": {"invalid": True}}})

    assert reviewer._review_state_blocked is True
    assert reviewer._review_state_block_reason == "review_data"
    assert reviewer._review_state_result is None


@pytest.mark.asyncio
async def test_run_publishes_state_transition_even_when_review_has_no_suggestions(monkeypatch):
    settings = _settings(monkeypatch)
    provider = MagicMock()
    provider.get_files.return_value = ["app.py"]
    provider.should_publish_review_as_thread.return_value = False
    reviewer = _reviewer(provider)
    reviewer.vars = {}
    reviewer._prepare_prediction = AsyncMock()
    reviewer._prepare_pr_review = MagicMock(return_value="No major issues detected")
    reviewer._review_state_result = SimpleNamespace(changed=True)
    reviewer._review_state_blocked = False

    async def fake_extract_tickets(git_provider, vars):
        return None

    async def fake_retry(prepare_fn, model_type=None):
        reviewer.prediction = "prediction"

    monkeypatch.setattr("pr_agent.tools.pr_reviewer.extract_and_cache_pr_tickets", fake_extract_tickets)
    monkeypatch.setattr("pr_agent.tools.pr_reviewer.retry_with_fallback_models", fake_retry)

    await reviewer.run()

    provider.publish_persistent_comment_full.assert_called_once()
    assert provider.publish_persistent_comment_full.call_args.kwargs["fallback_on_error"] is False
    assert provider.publish_persistent_comment_full.call_args.args[0] == "No major issues detected"
    provider.publish_persistent_comment.assert_not_called()
    assert settings.pr_reviewer.publish_output_no_suggestions is False


@pytest.mark.asyncio
async def test_invalid_history_updates_persistent_comment_without_fallback(monkeypatch):
    settings = _settings(monkeypatch)
    monkeypatch.setattr(settings.config, "is_auto_command", True)
    provider = MagicMock()
    provider.get_files.return_value = ["app.py"]
    provider.should_publish_review_as_thread.return_value = False
    reviewer = _reviewer(provider)
    reviewer.vars = {}
    reviewer._prepare_prediction = AsyncMock()
    reviewer._prepare_pr_review = MagicMock(return_value="No major issues detected")
    reviewer._review_state_result = None
    reviewer._review_state_blocked = True
    provider.get_issue_comments.return_value = [
        SimpleNamespace(body=f"{PRReviewHeader.REGULAR.value} 🔍\n\nold review\n\n<!-- pr-agent-review-state:v1\nbad\n-->")
    ]
    reviewer._load_review_finding_state()

    async def fake_extract_tickets(git_provider, vars):
        return None

    async def fake_retry(prepare_fn, model_type=None):
        reviewer.prediction = "prediction"

    monkeypatch.setattr("pr_agent.tools.pr_reviewer.extract_and_cache_pr_tickets", fake_extract_tickets)
    monkeypatch.setattr("pr_agent.tools.pr_reviewer.retry_with_fallback_models", fake_retry)

    await reviewer.run()

    provider.publish_persistent_comment_full.assert_called_once()
    assert provider.publish_persistent_comment_full.call_args.kwargs["fallback_on_error"] is False
    provider.publish_comment.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("block_reason", ["review_data", "read_error"])
async def test_non_marker_state_block_publishes_without_overwriting_state(monkeypatch, block_reason):
    _settings(monkeypatch)
    provider = MagicMock()
    provider.get_files.return_value = ["app.py"]
    provider.should_publish_review_as_thread.return_value = False
    reviewer = _reviewer(provider)
    reviewer.vars = {}
    reviewer._prepare_prediction = AsyncMock()
    reviewer._prepare_pr_review = MagicMock(return_value="review output")
    reviewer._review_state_result = None
    reviewer._review_state_blocked = True
    reviewer._review_state_block_reason = block_reason

    async def fake_extract_tickets(git_provider, vars):
        return None

    async def fake_retry(prepare_fn, model_type=None):
        reviewer.prediction = "prediction"

    monkeypatch.setattr("pr_agent.tools.pr_reviewer.extract_and_cache_pr_tickets", fake_extract_tickets)
    monkeypatch.setattr("pr_agent.tools.pr_reviewer.retry_with_fallback_models", fake_retry)

    await reviewer.run()

    provider.publish_comment.assert_any_call("review output")
    provider.publish_persistent_comment_full.assert_not_called()
