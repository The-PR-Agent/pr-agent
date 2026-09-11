from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.algo.finding_verifier import UNVERIFIED_HEADER_SUFFIX
from pr_agent.algo.types import FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer

REVIEW_YAML_TWO_ISSUES = """review:
  score: |
    80
  key_issues_to_review:
    - relevant_file: |
        lib/a.dart
      issue_header: |
        Refuted Issue
      issue_content: |
        Unless elsewhere, this is wrong.
      start_line: 1
      end_line: 2
    - relevant_file: |
        lib/b.dart
      issue_header: |
        Kept Issue
      issue_content: |
        The list is indexed past its length on line 4.
      start_line: 3
      end_line: 4
  security_concerns: |
    No
"""


def _make_reviewer(provider, ai_handler):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider
    reviewer.ai_handler = ai_handler
    reviewer.pr_url = "https://example.test/pull/1"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.prediction = "review: {}"
    reviewer.prediction_data = None
    reviewer.review_refuted_count = 0
    reviewer.review_unverified_count = 0
    reviewer.remaining_files_list = []
    reviewer.coverage = None
    reviewer.review_chunk_count = 1
    reviewer.review_failed_chunk_count = 0
    reviewer.review_vote_dropped_count = 0
    return reviewer


def _issues():
    return [
        {
            "relevant_file": "lib/a.dart",
            "start_line": 1,
            "end_line": 2,
            "issue_header": "Refuted Issue",
            "issue_content": "Unless elsewhere, this is wrong.",
        },
        {
            "relevant_file": "lib/b.dart",
            "start_line": 3,
            "end_line": 4,
            "issue_header": "Unverified Issue",
            "issue_content": "Maybe something is off.",
        },
        {
            "relevant_file": "lib/c.dart",
            "start_line": 5,
            "end_line": 6,
            "issue_header": "Confirmed Issue",
            "issue_content": "The list is indexed past its length on line 4.",
        },
    ]


def _enable_verification(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.pr_reviewer, "enable_finding_verification", True, raising=False)
    monkeypatch.setattr(settings.pr_reviewer, "verify_max_findings", 10, raising=False)
    monkeypatch.setattr(settings.pr_reviewer, "verification_model", "", raising=False)
    monkeypatch.setattr(settings.pr_reviewer, "verify_max_context_chars", 200000, raising=False)
    monkeypatch.setattr(settings.config, "model_weak", "weak-model", raising=False)
    monkeypatch.setattr(settings.config, "model", "strong-model", raising=False)
    return settings


@pytest.mark.asyncio
async def test_finding_verification_filters_refuted_and_tags_unverified(monkeypatch):
    _enable_verification(monkeypatch)

    provider = MagicMock()
    provider.last_commit_id = "head-sha"
    provider.get_diff_files.return_value = [
        FilePatchInfo(base_file="", head_file="", patch="", filename="lib/a.dart"),
        FilePatchInfo(base_file="", head_file="", patch="", filename="lib/b.dart"),
        FilePatchInfo(base_file="", head_file="", patch="", filename="lib/c.dart"),
    ]
    provider.get_pr_file_content = MagicMock(side_effect=lambda path, _sha: f"content:{path}")

    async def fake_chat_completion(model, system, user, temperature=0.0, stage=None, files=None, **_kwargs):
        assert model == "weak-model"
        assert stage == "verify"
        assert temperature == 0.0
        assert isinstance(files, list) and files
        if "Refuted Issue" in user:
            return '{"status":"refuted","evidence":"x","reason":"no"}', "stop"
        if "Unverified Issue" in user:
            return '{"status":"unverified","evidence":"","reason":"unclear"}', "stop"
        return '{"status":"confirmed","evidence":"y","reason":"yes"}', "stop"

    ai_handler = MagicMock()
    ai_handler.chat_completion = AsyncMock(side_effect=fake_chat_completion)

    reviewer = _make_reviewer(provider, ai_handler)
    reviewer.prediction_data = {"review": {"key_issues_to_review": _issues()}}

    await reviewer._verify_prediction_findings()

    kept = reviewer.prediction_data["review"]["key_issues_to_review"]
    headers = [issue["issue_header"] for issue in kept]
    assert "Refuted Issue" not in headers
    assert "Unverified Issue (unverified)" in headers
    assert "Confirmed Issue" in headers
    by_header = {issue["issue_header"]: issue for issue in kept}
    assert by_header["Unverified Issue (unverified)"]["verification"] == "unverified"
    assert by_header["Confirmed Issue"]["verification"] == "confirmed"
    assert reviewer.review_refuted_count == 1
    assert reviewer.review_unverified_count == 1
    assert ai_handler.chat_completion.await_count == 3


@pytest.mark.asyncio
async def test_finding_verification_skipped_when_flag_off(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.pr_reviewer, "enable_finding_verification", False, raising=False)

    provider = MagicMock()
    provider.get_pr_file_content = MagicMock()
    ai_handler = MagicMock()
    ai_handler.chat_completion = AsyncMock()

    reviewer = _make_reviewer(provider, ai_handler)
    reviewer.prediction_data = {"review": {"key_issues_to_review": _issues()}}

    await reviewer._verify_prediction_findings()

    assert ai_handler.chat_completion.await_count == 0
    assert provider.get_pr_file_content.call_count == 0
    assert len(reviewer.prediction_data["review"]["key_issues_to_review"]) == 3


@pytest.mark.asyncio
async def test_verification_pass_failure_keeps_all_findings(monkeypatch):
    _enable_verification(monkeypatch)

    provider = MagicMock()
    provider.get_diff_files.side_effect = RuntimeError("diff boom")
    ai_handler = MagicMock()
    ai_handler.chat_completion = AsyncMock()

    reviewer = _make_reviewer(provider, ai_handler)
    original = {"review": {"key_issues_to_review": _issues()}}
    reviewer.prediction_data = original

    await reviewer._verify_prediction_findings()

    assert reviewer.prediction_data is original
    assert len(reviewer.prediction_data["review"]["key_issues_to_review"]) == 3
    assert all(
        UNVERIFIED_HEADER_SUFFIX not in issue["issue_header"]
        for issue in reviewer.prediction_data["review"]["key_issues_to_review"]
    )
    assert reviewer.review_refuted_count == 0
    assert ai_handler.chat_completion.await_count == 0


@pytest.mark.asyncio
async def test_unverified_suffix_is_idempotent_and_stripped_for_state(monkeypatch):
    _enable_verification(monkeypatch)

    provider = MagicMock()
    provider.last_commit_id = "head-sha"
    provider.get_diff_files.return_value = [
        FilePatchInfo(base_file="", head_file="", patch="", filename="lib/b.dart"),
    ]
    provider.get_pr_file_content = MagicMock(return_value="content")

    async def fake_chat_completion(model, system, user, temperature=0.0, stage=None, files=None, **_kwargs):
        return '{"status":"unverified","evidence":"","reason":"unclear"}', "stop"

    ai_handler = MagicMock()
    ai_handler.chat_completion = AsyncMock(side_effect=fake_chat_completion)

    issue = {
        "relevant_file": "lib/b.dart",
        "start_line": 3,
        "end_line": 4,
        "issue_header": "Unverified Issue",
        "issue_content": "Maybe something is off.",
    }
    reviewer = _make_reviewer(provider, ai_handler)
    reviewer.prediction_data = {"review": {"key_issues_to_review": [issue]}}

    await reviewer._verify_prediction_findings()
    await reviewer._verify_prediction_findings()

    kept = reviewer.prediction_data["review"]["key_issues_to_review"][0]
    assert kept["issue_header"] == "Unverified Issue (unverified)"
    assert kept["issue_header"].count(UNVERIFIED_HEADER_SUFFIX) == 1

    finding = reviewer._review_finding_from_issue(kept)
    assert finding is not None
    assert UNVERIFIED_HEADER_SUFFIX not in finding["body"]
    assert "**Unverified Issue**" in finding["body"]


@pytest.mark.asyncio
async def test_run_publishes_without_refuted_finding(monkeypatch):
    settings = _enable_verification(monkeypatch)
    monkeypatch.setattr(settings.config, "publish_output", True)
    monkeypatch.setattr(settings.config, "is_auto_command", True, raising=False)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_comment", False)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_finding_state", False, raising=False)
    monkeypatch.setattr(settings.pr_reviewer, "inline_key_issues", False)
    monkeypatch.setattr(settings.pr_reviewer, "enable_review_coverage_footer", False, raising=False)
    monkeypatch.setattr(settings.pr_reviewer, "publish_output_no_suggestions", True)

    provider = MagicMock()
    provider.get_files.return_value = ["lib/a.dart", "lib/b.dart"]
    provider.last_commit_id = "head-sha"
    provider.get_diff_files.return_value = [
        FilePatchInfo(base_file="", head_file="", patch="", filename="lib/a.dart"),
        FilePatchInfo(base_file="", head_file="", patch="", filename="lib/b.dart"),
    ]
    provider.get_pr_file_content = MagicMock(side_effect=lambda path, _sha: f"content:{path}")
    provider.should_publish_review_as_thread.return_value = False
    provider.is_supported.return_value = False
    provider.publish_comment = MagicMock()
    published = []

    def capture_publish(body, *args, **kwargs):
        published.append(body)
        return SimpleNamespace(body=body)

    provider.publish_comment.side_effect = capture_publish

    async def fake_chat_completion(model, system, user, temperature=0.2, stage=None, files=None, **_kwargs):
        if stage == "verify":
            if "Refuted Issue" in user:
                return '{"status":"refuted","evidence":"x","reason":"no"}', "stop"
            return '{"status":"confirmed","evidence":"y","reason":"yes"}', "stop"
        return REVIEW_YAML_TWO_ISSUES, "stop"

    ai_handler = MagicMock()
    ai_handler.chat_completion = AsyncMock(side_effect=fake_chat_completion)

    reviewer = _make_reviewer(provider, ai_handler)
    reviewer.vars = {}
    reviewer._raw_prompt_vars = {}
    reviewer._review_state_result = None
    reviewer._review_state_blocked = False
    reviewer._review_state_block_reason = None

    async def fake_extract_tickets(git_provider, vars):
        return None

    async def fake_retry(prepare_fn, model_type=None, git_provider=None):
        # Simulate the review-stage model call, then parse like _prepare_prediction.
        response, _ = await reviewer.ai_handler.chat_completion(
            model="strong-model", system="s", user="u", temperature=0.2, stage="review"
        )
        reviewer.prediction = response
        reviewer.prediction_data = reviewer._load_review_yaml(response)

    monkeypatch.setattr("pr_agent.tools.pr_reviewer.extract_and_cache_pr_tickets", fake_extract_tickets)
    monkeypatch.setattr("pr_agent.tools.pr_reviewer.retry_with_fallback_models", fake_retry)

    with patch("pr_agent.tools.pr_reviewer.github_action_output"):
        await reviewer.run()

    assert reviewer.review_refuted_count == 1
    assert published, "expected a published review body"
    body = published[-1]
    assert "Refuted Issue" not in body
    assert "Kept Issue" in body
