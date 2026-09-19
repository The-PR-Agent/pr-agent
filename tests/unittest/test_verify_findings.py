"""Unit tests for the pr_reviewer.verify_findings second-pass gate."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.algo.run_details import get_run_details, init_run_details, record_model_used
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer

from ._settings_helpers import restore_settings, snapshot_settings

PREDICTION = """\
review:
  estimated_effort_to_review_[1-5]: 3
  key_issues_to_review:
    - relevant_file: src/a.py
      issue_header: Possible Bug
      issue_content: first issue
      start_line: 1
      end_line: 2
    - relevant_file: src/b.py
      issue_header: Possible Bug
      issue_content: second issue
      start_line: 3
      end_line: 4
"""

VERDICTS_DROP_SECOND = (
    "verdicts:\n"
    "  - issue: 1\n"
    "    supported: true\n"
    "    reason: present in diff\n"
    "  - issue: 2\n"
    "    supported: false\n"
    "    reason: code not in diff\n"
)


def _reviewer(verdicts: str) -> PRReviewer:
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.prediction = PREDICTION
    reviewer.prediction_data = None
    reviewer.patches_diff = "diff --git a/src/a.py b/src/a.py\n+new\n"
    reviewer.git_provider = None
    reviewer.ai_handler = MagicMock()
    reviewer.ai_handler.chat_completion = AsyncMock(return_value=(verdicts, "stop"))
    return reviewer


@pytest.fixture(autouse=True)
def verify_flag():
    snapshot = snapshot_settings(
        ["pr_reviewer.verify_findings", "config.model", "config.fallback_models",
         "config.custom_model_max_tokens"])
    get_settings().set("pr_reviewer.verify_findings", True)
    get_settings().set("config.model", "test-model")
    get_settings().set("config.fallback_models", [])
    # Without a window for the test model every verification attempt fails its
    # token budget before reaching the model call.
    get_settings().set("config.custom_model_max_tokens", 10_000)
    yield
    restore_settings(snapshot)


async def test_unsupported_findings_are_dropped() -> None:
    reviewer = _reviewer(VERDICTS_DROP_SECOND)
    await reviewer._verify_key_issues()
    issues = reviewer.prediction_data["review"]["key_issues_to_review"]
    assert len(issues) == 1
    assert issues[0]["relevant_file"] == "src/a.py"


async def test_gate_disabled_skips_model_call() -> None:
    get_settings().set("pr_reviewer.verify_findings", False)
    reviewer = _reviewer(VERDICTS_DROP_SECOND)
    await reviewer._verify_key_issues()
    reviewer.ai_handler.chat_completion.assert_not_called()
    assert reviewer.prediction_data is None


async def test_model_error_keeps_original_findings() -> None:
    reviewer = _reviewer(VERDICTS_DROP_SECOND)
    reviewer.ai_handler.chat_completion = AsyncMock(side_effect=RuntimeError("boom"))
    await reviewer._verify_key_issues()
    assert reviewer.prediction_data is None


async def test_verdict_without_list_keeps_all_findings() -> None:
    reviewer = _reviewer("verdicts: none\n")
    await reviewer._verify_key_issues()
    assert reviewer.prediction_data is None


async def test_missing_verdict_keeps_the_issue() -> None:
    reviewer = _reviewer("verdicts:\n  - issue: 1\n    supported: true\n")
    await reviewer._verify_key_issues()
    # No drop => prediction_data untouched, downstream uses the original prediction.
    assert reviewer.prediction_data is None


async def test_middle_missing_verdict_keeps_the_issue() -> None:
    # Verdict for issue 2 skipped entirely: only the explicit refute of 3 may drop.
    reviewer = _reviewer(
        "verdicts:\n"
        "  - issue: 1\n    supported: true\n"
        "  - issue: 3\n    supported: false\n"
    )
    reviewer.prediction = PREDICTION.replace(
        "    - relevant_file: src/b.py\n",
        "    - relevant_file: src/b.py\n"
        "      issue_header: Bug\n      issue_content: second\n"
        "      start_line: 3\n      end_line: 4\n"
        "    - relevant_file: src/c.py\n"
        "      issue_header: Bug\n      issue_content: third\n"
        "      start_line: 5\n      end_line: 6\n",
        1,
    )
    await reviewer._verify_key_issues()
    issues = reviewer.prediction_data["review"]["key_issues_to_review"]
    assert [i["relevant_file"] for i in issues] == ["src/a.py", "src/b.py"]


async def test_malformed_verdict_entry_is_ignored() -> None:
    reviewer = _reviewer(
        "verdicts:\n"
        "  - issue: 1\n    supported: true\n"
        "  - garbage\n"
        "  - issue: 2\n    supported: false\n"
    )
    await reviewer._verify_key_issues()
    issues = reviewer.prediction_data["review"]["key_issues_to_review"]
    assert [i["relevant_file"] for i in issues] == ["src/a.py"]


async def test_empty_issue_list_skips_model_call() -> None:
    reviewer = _reviewer(VERDICTS_DROP_SECOND)
    reviewer.prediction = "review:\n  key_issues_to_review: []\n"
    await reviewer._verify_key_issues()
    reviewer.ai_handler.chat_completion.assert_not_called()


async def test_float_and_bool_issue_ids_do_not_refute() -> None:
    # int() coercion would turn 1.9/true into 1; only exact ints may refute.
    reviewer = _reviewer(
        "verdicts:\n"
        "  - issue: 1.9\n    supported: false\n"
        "  - issue: true\n    supported: false\n"
        "  - issue: 2\n    supported: false\n"
    )
    await reviewer._verify_key_issues()
    issues = reviewer.prediction_data["review"]["key_issues_to_review"]
    assert [i["relevant_file"] for i in issues] == ["src/a.py"]


async def test_out_of_range_issue_id_is_ignored() -> None:
    reviewer = _reviewer("verdicts:\n  - issue: 99\n    supported: false\n")
    await reviewer._verify_key_issues()
    assert reviewer.prediction_data is None


async def test_merged_findings_skip_second_verification() -> None:
    # prediction_data set means the chunked path already verified per-chunk.
    reviewer = _reviewer(VERDICTS_DROP_SECOND)
    reviewer.prediction_data = {"review": {"key_issues_to_review": [{"issue": "x"}]}}
    await reviewer._verify_key_issues()
    reviewer.ai_handler.chat_completion.assert_not_called()


def _chunk_data(file_name: str) -> dict:
    return {"review": {"key_issues_to_review": [
        {"relevant_file": file_name, "issue_header": "Bug",
         "issue_content": f"issue in {file_name}", "start_line": 1, "end_line": 2}]}}


async def test_each_chunk_verified_against_its_own_diff() -> None:
    reviewer = _reviewer(VERDICTS_DROP_SECOND)
    reviewer._chunked_patches_diff_list = ["diff-chunk-A", "diff-chunk-B"]
    reviewer._chunked_results = {
        0: ("pred-A", _chunk_data("src/a.py"), "model"),
        1: ("pred-B", _chunk_data("src/b.py"), "model"),
    }
    await reviewer._verify_chunked_key_issues()
    calls = reviewer.ai_handler.chat_completion.call_args_list
    assert len(calls) == 2
    assert "diff-chunk-A" in calls[0].kwargs["user"]
    assert "diff-chunk-B" in calls[1].kwargs["user"]
    # Verdict refutes out-of-range issue 2 only: each single-issue chunk keeps its finding.
    assert len(reviewer._chunked_results[0][1]["review"]["key_issues_to_review"]) == 1
    assert len(reviewer._chunked_results[1][1]["review"]["key_issues_to_review"]) == 1


async def test_chunk_finding_survives_when_its_own_diff_supports_it() -> None:
    # Chunk A's verifier refutes its finding; chunk B's verifier supports it.
    reviewer = _reviewer(VERDICTS_DROP_SECOND)
    reviewer.ai_handler.chat_completion = AsyncMock(side_effect=[
        ("verdicts:\n  - issue: 1\n    supported: false\n", "stop"),
        ("verdicts:\n  - issue: 1\n    supported: true\n", "stop"),
    ])
    reviewer._chunked_patches_diff_list = ["diff-chunk-A", "diff-chunk-B"]
    reviewer._chunked_results = {
        0: ("pred-A", _chunk_data("src/a.py"), "model"),
        1: ("pred-B", _chunk_data("src/b.py"), "model"),
    }
    await reviewer._verify_chunked_key_issues()
    assert reviewer._chunked_results[0][1]["review"]["key_issues_to_review"] == []
    kept = reviewer._chunked_results[1][1]["review"]["key_issues_to_review"]
    assert [i["relevant_file"] for i in kept] == ["src/b.py"]


async def test_chunk_verification_error_keeps_that_chunk() -> None:
    reviewer = _reviewer(VERDICTS_DROP_SECOND)
    reviewer.ai_handler.chat_completion = AsyncMock(side_effect=RuntimeError("boom"))
    reviewer._chunked_patches_diff_list = ["diff-chunk-A"]
    reviewer._chunked_results = {0: ("pred-A", _chunk_data("src/a.py"), "model")}
    await reviewer._verify_chunked_key_issues()
    kept = reviewer._chunked_results[0][1]["review"]["key_issues_to_review"]
    assert [i["relevant_file"] for i in kept] == ["src/a.py"]


def test_verify_prompt_marks_payloads_as_untrusted() -> None:
    system = get_settings().pr_verify_findings_prompt.system
    assert "untrusted" in system


async def test_conflicting_verdicts_keep_the_finding() -> None:
    verdicts = (
        "verdicts:\n"
        "  - issue: 1\n"
        "    supported: true\n"
        "    reason: present in diff\n"
        "  - issue: 1\n"
        "    supported: false\n"
        "    reason: contradicts itself\n"
        "  - issue: 2\n"
        "    supported: false\n"
        "    reason: code not in diff\n"
    )
    reviewer = _reviewer(verdicts)
    await reviewer._verify_key_issues()
    issues = reviewer.prediction_data["review"]["key_issues_to_review"]
    assert [i["relevant_file"] for i in issues] == ["src/a.py"]


async def test_duplicate_false_verdict_keeps_the_finding() -> None:
    verdicts = (
        "verdicts:\n"
        "  - issue: 1\n"
        "    supported: false\n"
        "    reason: code not in diff\n"
        "  - issue: 1\n"
        "    supported: false\n"
        "    reason: code not in diff\n"
    )
    reviewer = _reviewer(verdicts)
    await reviewer._verify_key_issues()
    # Both entries agree but the contract demands one verdict per issue:
    # ambiguous output retains the finding, so nothing is dropped.
    assert reviewer.prediction_data is None


async def test_verification_fallback_keeps_the_review_model_attribution() -> None:
    init_run_details()
    record_model_used("test-model", is_fallback=False)
    get_settings().set("config.fallback_models", ["fallback-model"])
    reviewer = _reviewer(VERDICTS_DROP_SECOND)
    reviewer.ai_handler.chat_completion = AsyncMock(side_effect=[
        RuntimeError("primary unavailable"),
        (VERDICTS_DROP_SECOND, "stop"),
    ])
    await reviewer._verify_key_issues()
    assert reviewer.prediction_data is not None
    details = get_run_details()
    assert (details.model_used, details.fallback_used) == ("test-model", False)
