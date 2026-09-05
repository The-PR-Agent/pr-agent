import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.algo.review_finding_state import (
    parse_review_state,
    reconcile_review_findings,
    serialize_review_state,
)
from pr_agent.algo.utils import (
    PRReviewHeader,
    PRReviewIdentity,
    add_pr_review_identity,
    comment_matches_identity,
    get_pr_review_comment_identifiers,
)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.azuredevops_provider import AzureDevopsProvider
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider
from pr_agent.git_providers.bitbucket_server_provider import BitbucketServerProvider
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.gitea_provider import GiteaProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider
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
    provider.supports_review_finding_state.return_value = True
    provider.is_comment_authored_by_pr_agent.return_value = True
    provider.get_issue_comments_newest_first.side_effect = (
        lambda: list(reversed(provider.get_issue_comments()))
    )
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



def test_review_finding_state_is_disabled_without_a_provider(monkeypatch):
    _settings(monkeypatch)
    reviewer = PRReviewer.__new__(PRReviewer)

    assert reviewer._review_finding_state_enabled() is False


def test_prepare_review_reconciles_previous_state_and_renders_resolved_section(monkeypatch):
    settings = _settings(monkeypatch)
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        head_sha="head-1",
        timestamp="2026-01-01T00:00:00Z",
    ).state
    old_body = (
        f"{PRReviewHeader.REGULAR.value} 🔍\n\nold review\n\n"
        f"{serialize_review_state(previous)}"
    )
    provider = MagicMock()
    provider.last_commit_id = "head-2"
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


def test_prepare_review_same_head_absence_preserves_active_finding(monkeypatch):
    _settings(monkeypatch)
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        head_sha="head-1",
        timestamp="2026-01-01T00:00:00Z",
    ).state
    old_body = (
        f"{PRReviewHeader.REGULAR.value} 🔍\n\nold review\n\n"
        f"{serialize_review_state(previous)}"
    )
    provider = MagicMock()
    provider.last_commit_id = "head-1"
    provider.get_issue_comments.return_value = [SimpleNamespace(body=old_body)]
    provider.is_supported.side_effect = (
        lambda capability: capability == "get_issue_comments"
    )
    reviewer = _reviewer(provider)

    reviewer._prepare_review_finding_state(
        {"review": {"key_issues_to_review": []}}
    )

    finding = reviewer._review_state_result.state["findings"][0]
    assert finding["state"] == "ACTIVE"
    assert reviewer._review_state_result.resolved_ids == ()
    assert "resolved_at" not in finding
    assert "resolved_head_sha" not in finding


def test_prepare_review_pushes_final_markdown_with_lifecycle_state(monkeypatch):
    _settings(monkeypatch)
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        head_sha="head-1",
        timestamp="2026-01-01T00:00:00Z",
    ).state
    header = f"{PRReviewHeader.REGULAR.value} 🔍"
    provider = MagicMock()
    provider.last_commit_id = "head-2"
    provider.get_issue_comments.return_value = [
        SimpleNamespace(body=f"{header}\n\nold review\n\n{serialize_review_state(previous)}")
    ]
    provider.get_diff_files.return_value = []
    provider.is_supported.side_effect = lambda capability: capability == "get_issue_comments"
    reviewer = _reviewer(provider)

    with (
        patch("pr_agent.tools.pr_reviewer.load_yaml", return_value={"review": {"key_issues_to_review": []}}),
        patch("pr_agent.tools.pr_reviewer.github_action_output"),
        patch("pr_agent.tools.pr_reviewer.convert_to_markdown_v2", return_value="No major issues detected"),
        patch("pr_agent.tools.pr_reviewer.push_outputs") as push_outputs,
    ):
        review = reviewer._prepare_pr_review()

    assert "<summary>✅ Resolved findings</summary>" in review
    push_outputs.assert_called_once()
    assert push_outputs.call_args.kwargs["markdown"] == review
    assert "<summary>✅ Resolved findings</summary>" in push_outputs.call_args.kwargs["markdown"]


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



def _state_body(state, heading, identity=None):
    body = f"{heading}\n\nreview\n\n{serialize_review_state(state)}"
    return add_pr_review_identity(body, identity) if identity else body


def test_load_review_finding_state_accepts_default_heading_with_full_identity():
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    heading = f"{PRReviewHeader.REGULAR.value} 🔍"
    provider = MagicMock()
    provider.get_issue_comments.return_value = [
        SimpleNamespace(body=_state_body(previous, heading, PRReviewIdentity.REGULAR.value))
    ]
    reviewer = _reviewer(provider)

    parsed = reviewer._load_review_finding_state()

    assert parsed.valid is True
    assert parsed.state == previous


def test_load_review_finding_state_accepts_custom_heading_with_full_identity(monkeypatch):
    settings = _settings(monkeypatch)
    monkeypatch.setattr(settings.pr_reviewer, "review_heading", "Team Review", raising=False)
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    provider = MagicMock()
    provider.get_issue_comments.return_value = [
        SimpleNamespace(
            body=_state_body(
                previous,
                "## Team Review 🔍",
                PRReviewIdentity.REGULAR.value,
            )
        )
    ]
    reviewer = _reviewer(provider)

    parsed = reviewer._load_review_finding_state()

    assert parsed.valid is True
    assert parsed.state == previous


def test_full_identity_state_beats_legacy_visible_heading():
    old_state = reconcile_review_findings(
        None,
        [_finding("legacy")],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    new_state = reconcile_review_findings(
        None,
        [_finding("marked")],
        allow_resolution=True,
        timestamp="2026-01-01T00:01:00Z",
    ).state
    provider = MagicMock()
    provider.get_issue_comments.return_value = [
        SimpleNamespace(
            body=_state_body(
                old_state,
                f"{PRReviewHeader.REGULAR.value} 🔍",
            )
        ),
        SimpleNamespace(
            body=_state_body(
                new_state,
                "## Team Review 🔍",
                PRReviewIdentity.REGULAR.value,
            )
        ),
    ]
    reviewer = _reviewer(provider)

    parsed = reviewer._load_review_finding_state()

    assert parsed.valid is True
    assert parsed.state == new_state


def test_load_review_finding_state_does_not_adopt_incremental_identity():
    state = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    provider = MagicMock()
    provider.get_issue_comments.return_value = [
        SimpleNamespace(
            body=_state_body(
                state,
                "## Incremental Team Review 🔍",
                PRReviewIdentity.INCREMENTAL.value,
            )
        )
    ]
    reviewer = _reviewer(provider)

    parsed = reviewer._load_review_finding_state()

    assert parsed.valid is True
    assert parsed.present is False
    assert parsed.state is None


def test_load_review_finding_state_accepts_legacy_default_heading_without_identity():
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    provider = MagicMock()
    provider.get_issue_comments.return_value = [
        SimpleNamespace(
            body=_state_body(previous, f"{PRReviewHeader.REGULAR.value} 🔍")
        )
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
        head_sha="head-1",
        timestamp="2026-01-01T00:00:00Z",
    ).state
    comment = SimpleNamespace(
        body=f"{header}\n\nold review\n\n{serialize_review_state(previous)}"
    )
    provider = MagicMock()
    provider.last_commit_id = "head-2"
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

    identity_overhead = len(PRReviewIdentity.REGULAR.value) + 2
    assert reviewer._review_comment_max_chars() == 1000 - len(update_suffix) - identity_overhead


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
    kwargs = provider.publish_persistent_comment_full.call_args.kwargs
    assert kwargs["fallback_on_error"] is False
    assert kwargs["identity_marker"] == PRReviewIdentity.REGULAR.value
    assert kwargs["legacy_initial_header"] == f"{PRReviewHeader.REGULAR.value} 🔍"
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
    review_body = f"{PRReviewHeader.REGULAR.value} {chr(0x1F50D)}\n\nreview output"
    reviewer._prepare_pr_review = MagicMock(return_value=review_body)
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

    final_body = provider.publish_comment.call_args_list[-1].args[0]
    assert final_body.startswith("## Standalone PR Review\n")
    assert review_body in final_body
    assert PRReviewIdentity.REGULAR.value not in final_body
    assert not any(
        comment_matches_identity(final_body, identifier)
        for identifier in get_pr_review_comment_identifiers(full=True, incremental=False)
    )
    provider.publish_persistent_comment_full.assert_not_called()


def test_load_review_finding_state_rejects_spoofed_full_identity():
    previous = reconcile_review_findings(
        None,
        [_finding("spoofed state")],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    provider = MagicMock()
    provider.get_issue_comments.return_value = [
        SimpleNamespace(
            body=_state_body(
                previous,
                PRReviewHeader.REGULAR.value + " " + chr(0x1F50D),
                PRReviewIdentity.REGULAR.value,
            ),
            user=SimpleNamespace(login="human"),
        )
    ]
    reviewer = _reviewer(provider)
    provider.is_comment_authored_by_pr_agent.return_value = False

    parsed = reviewer._load_review_finding_state()

    assert parsed.present is False
    assert parsed.valid is True
    assert parsed.state is None


def test_load_review_finding_state_skips_newer_spoof_and_loads_older_agent_state():
    old_state = reconcile_review_findings(
        None,
        [_finding("old state")],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    spoofed_state = reconcile_review_findings(
        None,
        [_finding("spoofed state")],
        allow_resolution=True,
        timestamp="2026-01-01T00:01:00Z",
    ).state
    header = PRReviewHeader.REGULAR.value + " " + chr(0x1F50D)
    old = SimpleNamespace(
        body=_state_body(old_state, header, PRReviewIdentity.REGULAR.value),
        user=SimpleNamespace(login="agent"),
    )
    spoofed = SimpleNamespace(
        body=_state_body(spoofed_state, header, PRReviewIdentity.REGULAR.value),
        user=SimpleNamespace(login="human"),
    )
    provider = MagicMock()
    provider.get_issue_comments.return_value = [old, spoofed]
    reviewer = _reviewer(provider)
    provider.is_comment_authored_by_pr_agent.side_effect = (
        lambda comment: comment.user.login == "agent"
    )

    parsed = reviewer._load_review_finding_state()

    assert parsed.valid is True
    assert parsed.state == old_state


def test_legacy_state_requires_verified_comment_ownership():
    previous = reconcile_review_findings(
        None,
        [_finding("legacy state")],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    header = PRReviewHeader.REGULAR.value + " " + chr(0x1F50D)
    provider = MagicMock()
    provider.get_issue_comments.return_value = [
        SimpleNamespace(body=_state_body(previous, header), user=SimpleNamespace(login="human"))
    ]
    reviewer = _reviewer(provider)
    provider.is_comment_authored_by_pr_agent.return_value = False

    parsed = reviewer._load_review_finding_state()

    assert parsed.present is False
    assert parsed.state is None


def _large_review_issues(count=10):
    return [
        {
            "relevant_file": f"file-{index}.py",
            "issue_content": f"finding {index} " + ("x" * 120),
        }
        for index in range(count)
    ]


def test_oversized_new_state_keeps_review_publishable_without_marker(monkeypatch):
    settings = _settings(monkeypatch)
    monkeypatch.setattr(settings.pr_reviewer, "num_max_findings", 100)
    provider = MagicMock()
    provider.get_issue_comments.return_value = []
    provider.get_diff_files.return_value = []
    provider.is_supported.side_effect = lambda capability: capability == "get_issue_comments"
    provider.max_comment_chars = 500
    reviewer = _reviewer(provider)

    data = {"review": {"key_issues_to_review": _large_review_issues()}}
    with (
        patch("pr_agent.tools.pr_reviewer.load_yaml", return_value=data),
        patch("pr_agent.tools.pr_reviewer.github_action_output"),
        patch(
            "pr_agent.tools.pr_reviewer.convert_to_markdown_v2",
            return_value=PRReviewHeader.REGULAR.value + " " + chr(0x1F50D) + "\n\nhuman review",
        ),
        patch("pr_agent.tools.pr_reviewer.push_outputs") as push_outputs,
    ):
        review = reviewer._prepare_pr_review()

    assert "human review" in review
    assert parse_review_state(review).present is False
    assert reviewer._review_state_result is None
    assert reviewer._review_state_block_reason == "state_size"
    push_outputs.assert_called_once()
    assert push_outputs.call_args.kwargs["markdown"] == review


def test_oversized_new_state_preserves_previous_valid_marker(monkeypatch):
    settings = _settings(monkeypatch)
    monkeypatch.setattr(settings.pr_reviewer, "num_max_findings", 100)
    previous = reconcile_review_findings(
        None,
        [_finding("previous state")],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    header = PRReviewHeader.REGULAR.value + " " + chr(0x1F50D)
    old = SimpleNamespace(
        body=_state_body(previous, header, PRReviewIdentity.REGULAR.value),
        user=SimpleNamespace(login="agent"),
    )
    provider = MagicMock()
    provider.get_issue_comments.return_value = [old]
    provider.get_diff_files.return_value = []
    provider.is_supported.side_effect = lambda capability: capability == "get_issue_comments"
    provider.max_comment_chars = 500
    reviewer = _reviewer(provider)

    data = {"review": {"key_issues_to_review": _large_review_issues()}}
    with (
        patch("pr_agent.tools.pr_reviewer.load_yaml", return_value=data),
        patch("pr_agent.tools.pr_reviewer.github_action_output"),
        patch(
            "pr_agent.tools.pr_reviewer.convert_to_markdown_v2",
            return_value=header + "\n\n" + ("human review " * 100),
        ),
        patch("pr_agent.tools.pr_reviewer.push_outputs"),
    ):
        review = reviewer._prepare_pr_review()

    parsed = parse_review_state(review)
    assert parsed.valid is True
    assert parsed.state == previous
    assert reviewer._review_state_result is None
    assert reviewer._review_state_preserved is True
    assert reviewer._review_state_block_reason == "state_size"
    assert "human review" in review


@pytest.mark.asyncio
async def test_review_publish_uses_shared_full_signature_for_authorship(monkeypatch):
    _settings(monkeypatch)
    settings = get_settings()
    settings.config.is_auto_command = True
    settings.github.publish_as_check_run = False

    provider = GithubProvider.__new__(GithubProvider)
    provider.deployment_type = "user"
    provider.get_files = lambda: ["app.py"]
    provider.should_publish_review_as_thread = lambda: False
    provider.publish_comment = MagicMock()
    provider.publish_persistent_comment_full = MagicMock(return_value=object())

    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider
    reviewer.pr_url = "https://example.test/pull/1"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.remaining_files_list = []
    reviewer.vars = {}
    reviewer.prediction = None
    reviewer._review_state_result = None
    reviewer._review_state_blocked = False
    reviewer._review_state_block_reason = None
    reviewer._review_state_preserved = False
    reviewer._prepare_pr_review = MagicMock(return_value="review output")
    reviewer._should_publish_review_no_suggestions = lambda _review: True

    async def fake_extract_tickets(git_provider, vars):
        return None

    async def fake_retry(prepare_fn, model_type=None):
        reviewer.prediction = "prediction"

    monkeypatch.setattr(
        "pr_agent.tools.pr_reviewer.extract_and_cache_pr_tickets",
        fake_extract_tickets,
    )
    monkeypatch.setattr(
        "pr_agent.tools.pr_reviewer.retry_with_fallback_models",
        fake_retry,
    )

    await reviewer.run()

    provider.publish_persistent_comment_full.assert_called_once()
    kwargs = provider.publish_persistent_comment_full.call_args.kwargs
    assert kwargs["require_agent_authorship"] is True
    assert kwargs["fallback_on_error"] is False
    provider.publish_comment.assert_not_called()


@pytest.mark.parametrize(
    "provider_class",
    [
        GithubProvider,
        GitLabProvider,
        AzureDevopsProvider,
        GiteaProvider,
        BitbucketProvider,
        BitbucketServerProvider,
    ],
    ids=["github", "gitlab", "azure", "gitea", "bitbucket", "bitbucket-server"],
)
def test_legacy_persistent_publish_overrides_accept_shared_arguments(provider_class):
    parameters = inspect.signature(
        provider_class.publish_persistent_comment
    ).parameters

    assert "identity_marker" in parameters
    assert "legacy_initial_header" in parameters
    assert "require_agent_authorship" not in parameters
    assert "fallback_on_error" not in parameters


def test_oversized_state_degradation_is_safe_on_the_next_run(monkeypatch):
    settings = _settings(monkeypatch)
    monkeypatch.setattr(settings.pr_reviewer, "num_max_findings", 100)
    previous = reconcile_review_findings(
        None,
        [_finding("previous state")],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    header = PRReviewHeader.REGULAR.value + " " + chr(0x1F50D)
    old = SimpleNamespace(
        body=_state_body(previous, header, PRReviewIdentity.REGULAR.value),
        user=SimpleNamespace(login="agent"),
    )
    provider = MagicMock()
    provider.get_issue_comments.return_value = [old]
    provider.get_diff_files.return_value = []
    provider.is_supported.side_effect = lambda capability: capability == "get_issue_comments"
    provider.max_comment_chars = 500

    data = {"review": {"key_issues_to_review": _large_review_issues()}}

    def run_review(reviewer):
        with (
            patch("pr_agent.tools.pr_reviewer.load_yaml", return_value=data),
            patch("pr_agent.tools.pr_reviewer.github_action_output"),
            patch(
                "pr_agent.tools.pr_reviewer.convert_to_markdown_v2",
                return_value=header + "\n\n" + ("human review " * 100),
            ),
            patch("pr_agent.tools.pr_reviewer.push_outputs") as push_outputs,
        ):
            review = reviewer._prepare_pr_review()
        push_outputs.assert_called_once()
        return review

    first_reviewer = _reviewer(provider)
    first_review = run_review(first_reviewer)
    first_state = parse_review_state(first_review)
    assert first_state.valid is True
    assert first_state.state["findings"][0]["state"] == "ACTIVE"
    assert first_reviewer._review_state_block_reason == "state_size"

    provider.get_issue_comments.return_value = [
        SimpleNamespace(body=first_review, user=SimpleNamespace(login="agent"))
    ]
    second_reviewer = _reviewer(provider)
    second_review = run_review(second_reviewer)
    second_state = parse_review_state(second_review)

    assert second_state.valid is True
    assert second_state.state["findings"][0]["state"] == "ACTIVE"
    assert second_reviewer._review_state_block_reason == "state_size"


class _ReviewRunProvider:
    def __init__(self, comments=None, supports_state=False, authored=False, fail_persistent=False):
        self.comments = list(comments or [])
        self.supports_state = supports_state
        self.authored = authored
        self.fail_persistent = fail_persistent
        self.published = []
        self.edited = []
        self.removed = []
        self.persistent_calls = []

    def get_files(self):
        return ["app.py"]

    def should_publish_review_as_thread(self):
        return False

    def supports_review_comment_identity(self):
        return True

    def supports_review_finding_state(self):
        return self.supports_state

    def is_supported(self, capability):
        return capability == "get_issue_comments"

    def is_comment_authored_by_pr_agent(self, comment):
        return self.authored

    def get_issue_comments(self):
        return list(self.comments)

    def get_issue_comments_newest_first(self):
        return list(reversed(self.comments))

    def get_latest_commit_url(self):
        return "https://example.test/commit/1"

    def get_comment_url(self, comment):
        return "https://example.test/comment/1"

    def edit_comment(self, comment, body):
        if self.fail_persistent:
            return False
        comment.body = body
        self.edited.append((comment, body))
        return True

    def remove_comment(self, comment):
        self.removed.append(comment)

    def publish_comment(self, body, is_temporary=False, **kwargs):
        body = str(body)
        if (
            self.fail_persistent
            and not is_temporary
            and PRReviewIdentity.REGULAR.value in body
        ):
            return None
        result = SimpleNamespace(
            id=len(self.published) + 1,
            body=body,
            user=SimpleNamespace(login="agent"),
        )
        self.published.append((body, is_temporary, kwargs))
        return result

    def publish_persistent_comment(self, pr_comment, **kwargs):
        self.persistent_calls.append((pr_comment, kwargs))
        return self.publish_persistent_comment_full(pr_comment, **kwargs)

    def publish_persistent_comment_full(self, pr_comment, **kwargs):
        return GitProvider.publish_persistent_comment_full(self, pr_comment, **kwargs)


class _ReviewRunCheckProvider(_ReviewRunProvider):
    publish_persistent_comment = GithubProvider.publish_persistent_comment

    def __init__(self, check_run_result, comments=None):
        super().__init__(comments=comments, supports_state=False)
        self.check_run_result = check_run_result
        self.check_run_calls = []

    def _publish_check_run(self, body, name):
        self.check_run_calls.append((body, name))
        return self.check_run_result


def _reviewer_for_run(provider):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider
    reviewer.pr_url = "https://example.test/pull/1"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.remaining_files_list = []
    reviewer.vars = {}
    reviewer.prediction = None
    reviewer._review_state_result = None
    reviewer._review_state_blocked = False
    reviewer._review_state_block_reason = None
    reviewer._review_state_preserved = False
    reviewer._prepare_prediction = AsyncMock()
    review_body = f"{PRReviewHeader.REGULAR.value} {chr(0x1F50D)}\n\nreview output"
    reviewer._prepare_pr_review = MagicMock(return_value=review_body)
    reviewer._should_publish_review_no_suggestions = lambda _review: True
    return reviewer


def _patch_run_dependencies(monkeypatch, reviewer):
    async def fake_extract_tickets(git_provider, vars):
        return None

    async def fake_retry(prepare_fn, model_type=None):
        reviewer.prediction = "prediction"

    monkeypatch.setattr(
        "pr_agent.tools.pr_reviewer.extract_and_cache_pr_tickets",
        fake_extract_tickets,
    )
    monkeypatch.setattr(
        "pr_agent.tools.pr_reviewer.retry_with_fallback_models",
        fake_retry,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_class",
    [GiteaProvider, BitbucketProvider],
    ids=["gitea", "bitbucket"],
)
async def test_review_without_lifecycle_capability_uses_ordinary_persistent_review(
    monkeypatch, provider_class
):
    settings = _settings(monkeypatch)
    monkeypatch.setattr(settings.config, "is_auto_command", True, raising=False)
    provider = _ReviewRunProvider(supports_state=False)
    provider.supports_review_finding_state = (
        provider_class.supports_review_finding_state.__get__(
            provider,
            provider_class,
        )
    )
    provider.publish_persistent_comment = MagicMock(return_value=object())
    provider.publish_persistent_comment_full = MagicMock()
    reviewer = _reviewer_for_run(provider)
    _patch_run_dependencies(monkeypatch, reviewer)

    await reviewer.run()
    await reviewer.run()

    assert provider.publish_persistent_comment.call_count == 2
    provider.publish_persistent_comment_full.assert_not_called()
    assert provider.published == []
    for call in provider.publish_persistent_comment.call_args_list:
        review_body = call.args[0]
        assert not review_body.startswith("## Standalone PR Review\n")
        assert "<!-- pr-agent-review-state:" not in review_body
        assert "require_agent_authorship" not in call.kwargs
        assert "fallback_on_error" not in call.kwargs


@pytest.mark.asyncio
async def test_disabled_finding_state_uses_ordinary_persistent_review(monkeypatch):
    settings = _settings(monkeypatch)
    monkeypatch.setattr(settings.config, "is_auto_command", True, raising=False)
    monkeypatch.setattr(
        settings.pr_reviewer,
        "persistent_finding_state",
        False,
        raising=False,
    )
    provider = _ReviewRunProvider(supports_state=True, authored=True)
    provider.publish_persistent_comment = MagicMock(return_value=object())
    provider.publish_persistent_comment_full = MagicMock()
    reviewer = _reviewer_for_run(provider)
    _patch_run_dependencies(monkeypatch, reviewer)

    await reviewer.run()

    provider.publish_persistent_comment.assert_called_once()
    provider.publish_persistent_comment_full.assert_not_called()
    assert provider.published == []
    review_body = provider.publish_persistent_comment.call_args.args[0]
    assert not review_body.startswith("## Standalone PR Review\n")
    assert "<!-- pr-agent-review-state:" not in review_body

def test_github_check_setting_does_not_disable_non_check_provider_lifecycle(monkeypatch):
    settings = _settings(monkeypatch)
    monkeypatch.setattr(settings.github, "publish_as_check_run", True)
    monkeypatch.setattr(
        settings.azure_devops_server, "agent_identity",
        "11111111-1111-1111-1111-111111111111", raising=False,
    )
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    reviewer = _reviewer_for_run(provider)

    assert reviewer._review_finding_state_enabled() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("check_run_result", [True, False])
async def test_review_check_run_fallback_preserves_authorship_contract(
    monkeypatch, check_run_result
):
    settings = _settings(monkeypatch)
    monkeypatch.setattr(settings.config, "is_auto_command", True, raising=False)
    monkeypatch.setattr(settings.github, "publish_as_check_run", True)
    forged_body = add_pr_review_identity("forged review", PRReviewIdentity.REGULAR.value)
    forged = SimpleNamespace(body=forged_body, user=SimpleNamespace(login="human"))
    provider = _ReviewRunCheckProvider(check_run_result, comments=[forged])
    reviewer = _reviewer_for_run(provider)
    _patch_run_dependencies(monkeypatch, reviewer)

    await reviewer.run()

    review_body = reviewer._prepare_pr_review.return_value
    assert provider.check_run_calls == [(review_body, "review")]
    assert forged.body == forged_body
    assert provider.edited == []
    non_temporary = [
        body for body, is_temporary, _kwargs in provider.published if not is_temporary
    ]
    if check_run_result:
        assert non_temporary == []
    else:
        assert len(non_temporary) == 1
        standalone_review = non_temporary[0]
        assert standalone_review.startswith("## Standalone PR Review\n")
        assert review_body in standalone_review
        assert not any(
            comment_matches_identity(standalone_review, identifier)
            for identifier in get_pr_review_comment_identifiers(full=True, incremental=False)
        )


@pytest.mark.asyncio
async def test_review_run_does_not_edit_forged_persistent_comment_without_authorship(monkeypatch):
    _settings(monkeypatch)
    forged_body = add_pr_review_identity(
        "forged review", PRReviewIdentity.REGULAR.value
    )
    forged = SimpleNamespace(
        body=forged_body,
        user=SimpleNamespace(login="human"),
    )
    provider = _ReviewRunProvider(
        comments=[forged],
        supports_state=False,
        authored=False,
    )
    reviewer = _reviewer_for_run(provider)
    _patch_run_dependencies(monkeypatch, reviewer)

    await reviewer.run()

    assert forged.body == forged_body
    assert provider.edited == []
    assert provider.persistent_calls == []
    non_temporary = [
        body for body, is_temporary, _kwargs in provider.published if not is_temporary
    ]
    assert len(non_temporary) == 1
    standalone_review = non_temporary[0]
    assert standalone_review.startswith("## Standalone PR Review\n")
    assert reviewer._prepare_pr_review.return_value in standalone_review
    assert PRReviewIdentity.REGULAR.value not in standalone_review
    assert not any(
        comment_matches_identity(standalone_review, identifier)
        for identifier in get_pr_review_comment_identifiers(full=True, incremental=False)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("is_auto_command", [False, True])
async def test_review_run_surfaces_failed_persistent_write(monkeypatch, is_auto_command):
    settings = _settings(monkeypatch)
    monkeypatch.setattr(settings.config, "is_auto_command", is_auto_command, raising=False)
    old_body = add_pr_review_identity(
        "previous review", PRReviewIdentity.REGULAR.value
    )
    old_comment = SimpleNamespace(
        body=old_body,
        user=SimpleNamespace(login="agent"),
    )
    provider = _ReviewRunProvider(
        comments=[old_comment],
        supports_state=True,
        authored=True,
        fail_persistent=True,
    )
    reviewer = _reviewer_for_run(provider)
    reviewer._review_state_result = SimpleNamespace(changed=True)
    _patch_run_dependencies(monkeypatch, reviewer)

    await reviewer.run()

    assert old_comment.body == old_body
    non_temporary = [
        body for body, is_temporary, _kwargs in provider.published if not is_temporary
    ]
    assert non_temporary == ["Failed to review PR"]
    assert not any(
        comment_matches_identity(non_temporary[0], identifier)
        for identifier in get_pr_review_comment_identifiers(full=True, incremental=False)
    )


def test_persistent_publish_success_rejects_none_and_false():
    assert PRReviewer._persistent_publish_succeeded(None) is False
    assert PRReviewer._persistent_publish_succeeded(False) is False
    assert PRReviewer._persistent_publish_succeeded(object()) is True



@pytest.mark.asyncio
@pytest.mark.parametrize("status_result", [None, False])
async def test_successful_persistence_is_not_failed_by_optional_status_result(
    monkeypatch, status_result
):
    settings = _settings(monkeypatch)
    monkeypatch.setattr(settings.config, "is_auto_command", True, raising=False)
    monkeypatch.setattr(settings.pr_reviewer, "final_update_message", True)
    old_body = add_pr_review_identity("old review", PRReviewIdentity.REGULAR.value)
    old_comment = SimpleNamespace(body=old_body)

    class StatusNoticeProvider(_ReviewRunProvider):
        def publish_comment(self, body, is_temporary=False, **kwargs):
            if body.startswith("**[Persistent review]"):
                self.status_attempts += 1
                return status_result
            return super().publish_comment(body, is_temporary=is_temporary, **kwargs)

    provider = StatusNoticeProvider(
        comments=[old_comment], supports_state=True, authored=True,
    )
    provider.status_attempts = 0
    reviewer = _reviewer_for_run(provider)
    reviewer._review_state_result = SimpleNamespace(changed=True)
    _patch_run_dependencies(monkeypatch, reviewer)

    await reviewer.run()

    assert len(provider.edited) == 1
    assert old_comment.body != old_body
    assert provider.status_attempts == 1
    assert provider.published == []


@pytest.mark.asyncio
async def test_persistent_publish_exception_is_visible_for_auto_review(monkeypatch):
    settings = _settings(monkeypatch)
    monkeypatch.setattr(settings.config, "is_auto_command", True, raising=False)
    monkeypatch.setattr(settings.config, "propagate_tool_errors", False, raising=False)
    old_body = add_pr_review_identity("old review", PRReviewIdentity.REGULAR.value)
    old_comment = SimpleNamespace(body=old_body)

    class RaisingPersistentProvider(_ReviewRunProvider):
        def publish_persistent_comment_full(self, pr_comment, **kwargs):
            raise RuntimeError("persistent publication failed")

    provider = RaisingPersistentProvider(
        comments=[old_comment], supports_state=True, authored=True,
    )
    reviewer = _reviewer_for_run(provider)
    reviewer._review_state_result = SimpleNamespace(changed=True)
    _patch_run_dependencies(monkeypatch, reviewer)

    await reviewer.run()

    assert old_comment.body == old_body
    assert provider.edited == []
    assert [body for body, temporary, _ in provider.published if not temporary] == [
        "Failed to review PR",
    ]
