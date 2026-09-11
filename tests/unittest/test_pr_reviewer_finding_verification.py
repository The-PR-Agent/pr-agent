from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.algo.types import FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer


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


@pytest.mark.asyncio
async def test_finding_verification_filters_refuted_and_tags_unverified(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.pr_reviewer, "enable_finding_verification", True, raising=False)
    monkeypatch.setattr(settings.pr_reviewer, "verify_max_findings", 10, raising=False)
    monkeypatch.setattr(settings.pr_reviewer, "verification_model", "", raising=False)
    monkeypatch.setattr(settings.config, "model_weak", "weak-model", raising=False)
    monkeypatch.setattr(settings.config, "model", "strong-model", raising=False)

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
        assert files
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
