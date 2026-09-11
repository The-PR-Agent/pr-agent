import json
from pathlib import Path

from tests.eval.labels import LabeledFinding, LabelSet, load_labels, score_labels


def _labels() -> LabelSet:
    return LabelSet(
        repo="samer2373/block_rush", pr=1, head_sha="6e6138953716182466d6a6d448e2e75f59fc76fd",
        findings=(
            LabeledFinding(id="iap-dup-grant", path="lib/src/iap/iap_providers.dart", line_start=130, line_end=134,
                           label="TP", category="business", severity=3, summary="double credit",
                           signals=("complete", "redeliver", "idempot")),
            LabeledFinding(id="ads-show-timeout", path="lib/src/ads/ads_service.dart", line_start=135, line_end=167,
                           label="MISSED", category="correctness", severity=4, summary="show path no timeout",
                           signals=("timeout", "never complete", "soft-lock", "stuck")),
            LabeledFinding(id="history-dup", path="lib/src/profile/profile_providers.dart",
                           line_start=116, line_end=120, label="FP", category="business", severity=0,
                           summary="duplicate history is not real",
                           signals=("duplicate", "twice", "two entries")),
            LabeledFinding(id="control-baseline", path="lib/src/profile/profile_providers.dart", line_start=85,
                           line_end=112, label="CONTROL", category="correctness", severity=0,
                           summary="baseline fold is correct", signals=("baseline", "idempot")),
        ),
    )


def _review(*issues: dict) -> dict:
    return {"review": {"key_issues_to_review": list(issues)}}


def test_tp_matched_and_missed_counted():
    review = _review({"relevant_file": "lib/src/iap/iap_providers.dart", "start_line": 131, "end_line": 133,
                      "issue_header": "Duplicate Grant", "issue_content": "store will redeliver, no idempotency"})
    report = score_labels(_labels(), review)
    assert report.matched == ["iap-dup-grant"]
    assert "ads-show-timeout" in report.missed
    assert report.recall == 0.5  # 1 of 2 expected (TP + MISSED)
    assert report.severity_weighted_recall == 3 / 7


def test_fp_label_hit_counts_as_false_flag():
    review = _review({"relevant_file": "lib/src/profile/profile_providers.dart", "start_line": 117, "end_line": 118,
                      "issue_header": "Duplicate History", "issue_content": "Recent Runs shows two entries"})
    report = score_labels(_labels(), review)
    assert report.false_flags == ["history-dup"]
    assert report.precision == 0.0


def test_control_region_flagged_is_false_flag():
    review = _review({"relevant_file": "lib/src/profile/profile_providers.dart", "start_line": 90, "end_line": 95,
                      "issue_header": "Bug", "issue_content": "baseline is not idempotent"})
    report = score_labels(_labels(), review)
    assert report.control_false_flags == ["control-baseline"]


def test_unlabeled_finding_is_unknown_not_false():
    review = _review({"relevant_file": "lib/main.dart", "start_line": 1, "end_line": 2,
                      "issue_header": "X", "issue_content": "something new"})
    report = score_labels(_labels(), review)
    assert report.unknown == 1
    assert report.false_flags == []


def test_load_labels_roundtrip(tmp_path: Path):
    path = tmp_path / "labels.json"
    path.write_text(json.dumps({
        "repo": "o/r", "pr": 9, "head_sha": "abc",
        "findings": [{"id": "a", "path": "x.py", "line_start": 1, "line_end": 2, "label": "TP",
                      "category": "correctness", "severity": 2, "summary": "s", "signals": ["boom"]}]}))
    labels = load_labels(path)
    assert labels.findings[0].signals == ("boom",)
