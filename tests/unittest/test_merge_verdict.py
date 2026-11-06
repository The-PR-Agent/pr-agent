from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.algo import merge_verdict as mv
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.tools.pr_reviewer import PRReviewer, _with_merge_verdict
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

CLEAN_RISK = {"has_findings": False, "sensitive_paths": [], "risky_shapes": [], "deleted_files": []}


def _provider(ci="passed", stale=False, human=True):
    return SimpleNamespace(
        get_head_ci_status=lambda: ci,
        is_head_stale_against_base=lambda: stale,
        are_all_authors_human=lambda: human,
    )


def _files(added=10, removed=0):
    return [
        FilePatchInfo(
            base_file="",
            head_file="",
            patch="",
            filename="app.py",
            edit_type=EDIT_TYPE.MODIFIED,
            num_plus_lines=added,
            num_minus_lines=removed,
        )
    ]


def _verdict(**overrides):
    values = {
        "git_provider": _provider(),
        "diff_files": _files(),
        "model_recommendation": "no_concerns_found",
        "model_risk_level": "low",
        "risk_checks": CLEAN_RISK,
        "max_changed_lines": 100,
    }
    values.update(overrides)
    return mv.compute_merge_verdict(**values)


def _failed(result):
    return [check["name"] for check in result["checks"] if not check["passed"]]


def test_all_checks_pass_publishes_no_concerns_found():
    result = _verdict()
    assert result["recommendation"] == "no_concerns_found"
    assert _failed(result) == []
    assert result["decided_by"] == []
    assert [check["name"] for check in result["checks"]] == [
        "CI on reviewed head",
        "Head is current with its base",
        "Human authors",
        "No sensitive paths or risky changes",
        "Changed lines within cap",
    ]


@pytest.mark.parametrize(
    ("overrides", "failed_name"),
    [
        ({"git_provider": _provider(ci="pending")}, "CI on reviewed head"),
        ({"git_provider": _provider(ci=None)}, "CI on reviewed head"),
        ({"git_provider": _provider(stale=True)}, "Head is current with its base"),
        ({"git_provider": _provider(stale=None)}, "Head is current with its base"),
        ({"git_provider": _provider(human=False)}, "Human authors"),
        ({"git_provider": _provider(human=None)}, "Human authors"),
        ({"risk_checks": None}, "No sensitive paths or risky changes"),
        ({"risk_checks": {"has_findings": True, "sensitive_paths": [{"file": "a"}]}},
         "No sensitive paths or risky changes"),
        ({"diff_files": _files(added=101)}, "Changed lines within cap"),
    ],
)
def test_any_failing_or_unavailable_check_blocks_a_clean_verdict(overrides, failed_name):
    result = _verdict(**overrides)
    assert result["recommendation"] == "needs_review"
    assert failed_name in result["decided_by"]
    assert failed_name in _failed(result)


def test_missing_provider_methods_count_as_failed():
    result = _verdict(git_provider=SimpleNamespace())
    assert result["recommendation"] == "needs_review"
    assert "CI on reviewed head" in result["decided_by"]
    assert "Head is current with its base" in result["decided_by"]
    assert "Human authors" in result["decided_by"]


def test_model_recommendation_cannot_upgrade_a_failed_check():
    result = _verdict(git_provider=_provider(ci="failed"), model_recommendation="no_concerns_found")
    assert result["recommendation"] == "needs_review"
    assert "Model merge recommendation" not in _failed(result)


def test_model_recommendation_vetoes_a_clean_verdict():
    result = _verdict(model_recommendation="needs_review")
    assert result["recommendation"] == "needs_review"
    assert "Model merge recommendation" in _failed(result)
    assert "model merge recommendation: needs_review" in result["decided_by"]


def test_model_high_risk_vetoes_a_clean_verdict():
    result = _verdict(model_risk_level="high")
    assert result["recommendation"] == "changes_required"
    assert "Model risk level" in _failed(result)


def test_model_cannot_raise_a_blocked_verdict():
    result = _verdict(
        git_provider=_provider(ci="pending"),
        model_recommendation="no_concerns_found",
        model_risk_level="low",
    )
    assert result["recommendation"] == "needs_review"


def test_model_values_outside_the_schema_do_not_veto():
    result = _verdict(model_recommendation="approve", model_risk_level="critical")
    assert result["recommendation"] == "no_concerns_found"


def test_changed_lines_uses_patch_when_provider_counts_are_unknown():
    file = FilePatchInfo(
        base_file="",
        head_file="",
        patch="@@ -1,2 +1,3 @@\n old\n+new\n+another\n",
        filename="app.py",
    )
    assert mv.count_changed_lines([file]) == 2


def test_changed_lines_is_none_for_a_missing_patch():
    file = FilePatchInfo(base_file="", head_file="", patch=None, filename="app.py")
    assert mv.count_changed_lines([file]) is None


def test_invalid_changed_lines_cap_fails_the_check(monkeypatch):
    settings = get_settings()
    snapshot = snapshot_settings(["pr_reviewer.max_changed_lines"])
    try:
        settings.set("pr_reviewer.max_changed_lines", "many")
        result = _verdict(max_changed_lines=None)
        assert "Changed lines within cap" in result["decided_by"]
    finally:
        restore_settings(snapshot)


def test_render_checks_for_gfm_and_plain_markdown():
    checks = [
        {"name": "CI on reviewed head", "passed": True, "detail": "passed"},
        {"name": "Human authors", "passed": False, "detail": "bot or unlinked author"},
    ]
    gfm = mv.render_merge_recommendation_checks(checks, True)
    assert "Merge recommendation checks" in gfm
    assert "CI on reviewed head: passed" in gfm
    assert "Human authors: bot or unlinked author" in gfm
    plain = mv.render_merge_recommendation_checks(checks, False)
    assert plain.startswith("### Merge recommendation checks")
    assert "- ✅" in plain and "- ⚠️" in plain


def test_with_merge_verdict_keeps_checks_next_to_the_recommendation():
    review = {"risk_level": "low", "merge_recommendation": "no_concerns_found", "key_issues_to_review": []}
    result = _verdict()
    updated = _with_merge_verdict(review, result)
    assert list(updated) == [
        "risk_level",
        "merge_recommendation",
        "merge_recommendation_checks",
        "key_issues_to_review",
    ]
    assert updated["merge_recommendation"] == "no_concerns_found"


@pytest.mark.parametrize(
    ("recommendation", "conclusion"),
    [
        ("no_concerns_found", "success"),
        ("needs_review", "neutral"),
        ("changes_required", "failure"),
    ],
)
def test_github_publishes_the_verdict_conclusion(recommendation, conclusion):
    provider = GithubProvider.__new__(GithubProvider)
    provider._upsert_check_run = MagicMock(return_value=True)
    assert provider.publish_merge_verdict_check(recommendation) is True
    name, body = provider._upsert_check_run.call_args.args
    assert name == "verdict"
    assert body["conclusion"] == conclusion


def test_github_rejects_an_unknown_verdict():
    provider = GithubProvider.__new__(GithubProvider)
    provider._upsert_check_run = MagicMock(return_value=True)
    assert provider.publish_merge_verdict_check("approve") is False
    provider._upsert_check_run.assert_not_called()


def test_verdict_check_is_not_published_by_default():
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = SimpleNamespace(publish_merge_verdict_check=MagicMock(return_value=True))
    reviewer._computed_merge_verdict = {"recommendation": "no_concerns_found"}
    assert reviewer._publish_merge_verdict_check() is False
    reviewer.git_provider.publish_merge_verdict_check.assert_not_called()


def test_verdict_check_publishes_the_computed_recommendation():
    settings = get_settings()
    snapshot = snapshot_settings(["github.publish_merge_verdict_as_check"])
    try:
        settings.set("github.publish_merge_verdict_as_check", True)
        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.git_provider = SimpleNamespace(publish_merge_verdict_check=MagicMock(return_value=True))
        reviewer._computed_merge_verdict = {"recommendation": "changes_required"}
        assert reviewer._publish_merge_verdict_check() is True
        reviewer.git_provider.publish_merge_verdict_check.assert_called_once_with("changes_required")
    finally:
        restore_settings(snapshot)
