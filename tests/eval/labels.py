"""Adjudicated labels for a real PR: what the reviewer should and should not have said.

A label is TP (the tool said it and it is real), FP (the tool said it and it is wrong),
OVERSTATED (real but severity wrong; scored as TP for recall, flagged separately),
MISSED (real, tool silent), or CONTROL (correct code that must not be flagged).
Recall is measured over TP + OVERSTATED + MISSED; a hit on an FP or CONTROL region is a false flag.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from pr_agent.algo.review_merge import finding_line_range, line_ranges_overlap
from tests.eval.scoring import _finding_text, _same_file

Label = Literal["TP", "FP", "OVERSTATED", "MISSED", "CONTROL"]
POSITIVE_LABELS = ("TP", "OVERSTATED", "MISSED")
NEGATIVE_LABELS = ("FP", "CONTROL")


@dataclass(frozen=True)
class LabeledFinding:
    id: str
    path: str
    line_start: int
    line_end: int
    label: Label
    category: str
    severity: int  # 0 for negatives, 1-4 for positives
    summary: str
    signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class LabelSet:
    repo: str
    pr: int
    head_sha: str
    findings: tuple[LabeledFinding, ...]


@dataclass
class LabelReport:
    matched: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    false_flags: list[str] = field(default_factory=list)
    control_false_flags: list[str] = field(default_factory=list)
    unknown: int = 0
    total_findings: int = 0
    expected_positive: int = 0
    expected_severity: int = 0
    matched_severity: int = 0

    @property
    def recall(self) -> float:
        return len(self.matched) / self.expected_positive if self.expected_positive else 0.0

    @property
    def severity_weighted_recall(self) -> float:
        return self.matched_severity / self.expected_severity if self.expected_severity else 0.0

    @property
    def precision(self) -> float:
        judged = len(self.matched) + len(self.false_flags) + len(self.control_false_flags)
        return len(self.matched) / judged if judged else 0.0

    def as_dict(self) -> dict:
        data = asdict(self)
        data.update(precision=self.precision, recall=self.recall,
                    severity_weighted_recall=self.severity_weighted_recall)
        return data


def load_labels(path: str | Path) -> LabelSet:
    raw = json.loads(Path(path).read_text())
    findings = tuple(
        LabeledFinding(**{**item, "signals": tuple(s.lower() for s in item.get("signals", []))})
        for item in raw["findings"]
    )
    return LabelSet(repo=raw["repo"], pr=int(raw["pr"]), head_sha=raw["head_sha"], findings=findings)


def _hits(label: LabeledFinding, finding: dict) -> bool:
    if not _same_file(str(finding.get("relevant_file", "")), (label.path,)):
        return False
    rng = finding_line_range(finding)
    overlaps = rng is not None and line_ranges_overlap(rng, (label.line_start, label.line_end))
    text = _finding_text(finding)
    signal = any(s in text for s in label.signals) if label.signals else False
    return overlaps and (signal or not label.signals)


def score_labels(labels: LabelSet, review: dict | None) -> LabelReport:
    report = LabelReport()
    positives = [f for f in labels.findings if f.label in POSITIVE_LABELS]
    report.expected_positive = len(positives)
    report.expected_severity = sum(f.severity for f in positives)
    findings = list(((review or {}).get("review") or {}).get("key_issues_to_review") or [])
    report.total_findings = len(findings)
    matched_ids: set[str] = set()
    for finding in findings:
        hit_any = False
        for label in labels.findings:
            if not _hits(label, finding):
                continue
            hit_any = True
            if label.label in POSITIVE_LABELS and label.id not in matched_ids:
                matched_ids.add(label.id)
                report.matched.append(label.id)
                report.matched_severity += label.severity
            elif label.label == "FP" and label.id not in report.false_flags:
                report.false_flags.append(label.id)
            elif label.label == "CONTROL" and label.id not in report.control_false_flags:
                report.control_false_flags.append(label.id)
        if not hit_any:
            report.unknown += 1
    report.missed = [f.id for f in positives if f.id not in matched_ids]
    return report
