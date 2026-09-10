# P0: Measure and Fix the Review Loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/review` measurable (labeled corpus, per-call ledger, line-level coverage) and then fix the six loop defects the block_rush audit exposed: hidden findings, optimistic resolution, exact-hash identity, unrecovered chunk failures, unverified cross-file claims, and budget spent on unshipped files.

**Architecture:** Measurement lands first as pure-Python modules with no model calls (`tests/eval/labels.py`, `pr_agent/algo/run_ledger.py`, `pr_agent/algo/review_coverage.py`) and is threaded into `PRReviewer` behind config flags that default to current behavior. Loop fixes then change `review_finding_state.py` (schema v2, `UNCONFIRMED`, carried rendering), `review_merge.py` (cross-run matcher), `pr_reviewer.py` (recovery, verification, priority ordering), and add one new prompt file for verification. Every task ends with a unit test; Task 10 records the corpus baseline that later tiers must beat.

**Tech Stack:** Python 3.12, Dynaconf settings, Jinja2 prompts (`StrictUndefined`), pytest (`asyncio_mode=auto`), litellm handler, existing `tests/eval` harness (`PlainDiffGitProvider`, `score_defect`).

**Spec:** `docs/superpowers/specs/2026-09-10-review-quality-requirements.md` (requirements R-1 … R-9). Audit report: https://claude.ai/code/artifact/ae4951fa-5af8-47e3-b3ce-3ed1e346b23b

## Global Constraints

- Python ≥ 3.12; install with `uv sync`; run tests as `PYTHONPATH=. uv run pytest tests/unittest/<file> -q`.
- New config keys go in `pr_agent/settings/configuration.toml` under `[pr_reviewer]` (or `[config]` where noted) with a comment, and default to today's behavior.
- New prompt file `pr_agent/settings/pr_finding_verifier_prompts.toml` must be added to `settings_files=[...]` in `pr_agent/config_loader.py`.
- Jinja prompts use `StrictUndefined`: every variable referenced must be present in `vars`.
- Ruff rules `E,F,B,I`; 120-char lines; run `uv run ruff check --fix <touched files>` before each commit; do not run `ruff format`.
- Commit messages: Conventional Commits, end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Never commit secrets; the block_rush diff fixture is fetched on demand, not committed (see Task 1).
- Uncommitted work already on the branch (`findings_layout` in `utils.py`, `configuration.toml`, `test_convert_to_markdown.py`) must be committed first (Task 0).

---

### Task 0: Land the pending `findings_layout` work

**Files:**
- Modify (already modified, uncommitted): `pr_agent/algo/utils.py`, `pr_agent/settings/configuration.toml`, `tests/unittest/test_convert_to_markdown.py`

- [ ] **Step 1: Run the existing test file**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_convert_to_markdown.py -q`
Expected: PASS (all tests, including `TestFindingsLayout`)

- [ ] **Step 2: Lint and commit**

```bash
uv run ruff check --fix pr_agent/algo/utils.py tests/unittest/test_convert_to_markdown.py
git add pr_agent/algo/utils.py pr_agent/settings/configuration.toml tests/unittest/test_convert_to_markdown.py
git commit -m "feat(review): add findings_layout option for expanded key issues

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 1: Labeled real-PR corpus and scorer (R-1)

**Files:**
- Create: `tests/eval/labels.py`
- Create: `tests/eval/labels/block_rush_pr1.json`
- Create: `tests/eval/fetch_pr_diff.sh`
- Modify: `tests/eval/run_eval.py:207-220` (add `--labels`, `--diff-file`)
- Modify: `tests/eval/README.md` (usage section)
- Test: `tests/unittest/test_eval_labels.py`

**Interfaces:**
- Consumes: `tests/eval/scoring.py::_same_file(finding_file, defect_files)`, `_overlaps_defect(finding, ranges)`, `_finding_text(finding)`.
- Produces: `LabeledFinding` dataclass; `load_labels(path) -> LabelSet`; `score_labels(label_set, review: dict | None) -> LabelReport`; `LabelReport.as_dict()` with keys `precision, recall, severity_weighted_recall, control_false_flags, matched, missed, false_flags`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unittest/test_eval_labels.py
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
            LabeledFinding(id="history-dup", path="lib/src/profile/profile_providers.dart", line_start=116, line_end=120,
                           label="FP", category="business", severity=0, summary="duplicate history is not real",
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_eval_labels.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.eval.labels'`

- [ ] **Step 3: Write the module**

```python
# tests/eval/labels.py
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

from tests.eval.scoring import _finding_text, _same_file

Label = Literal["TP", "FP", "OVERSTATED", "MISSED", "CONTROL"]
POSITIVE_LABELS = ("TP", "OVERSTATED", "MISSED")
NEGATIVE_LABELS = ("FP", "CONTROL")
LINE_TOLERANCE = 3


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


def _finding_range(finding: dict) -> tuple[int, int] | None:
    try:
        start = int(finding.get("start_line") or 0)
        end = int(finding.get("end_line") or start)
    except (TypeError, ValueError):
        return None
    return (start, end) if start else None


def _hits(label: LabeledFinding, finding: dict) -> bool:
    if not _same_file(str(finding.get("relevant_file", "")), (label.path,)):
        return False
    rng = _finding_range(finding)
    overlaps = rng is not None and rng[0] <= label.line_end + LINE_TOLERANCE and rng[1] >= label.line_start - LINE_TOLERANCE
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_eval_labels.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Write the block_rush label file**

Transcribe every row of the audit report's sections 1 and 2 into `tests/eval/labels/block_rush_pr1.json`. Use this exact shape; the first four entries are given, add the remaining 25 (7 more tool verdicts, 16 more misses, 2 more controls) from the report:

```json
{
  "repo": "samer2373/block_rush",
  "pr": 1,
  "head_sha": "6e6138953716182466d6a6d448e2e75f59fc76fd",
  "findings": [
    {"id": "iap-dup-grant", "path": "lib/src/iap/iap_providers.dart", "line_start": 130, "line_end": 134,
     "label": "TP", "category": "business", "severity": 3, "summary": "consumable coin packs double-credited on redelivery",
     "signals": ["redeliver", "idempot", "complete", "double"]},
    {"id": "iap-unhandled-listen", "path": "lib/src/iap/iap_providers.dart", "line_start": 201, "line_end": 207,
     "label": "TP", "category": "correctness", "severity": 3, "summary": "purchase stream listener discards async future",
     "signals": ["unhandled", "await", "onerror", "future"]},
    {"id": "history-dup", "path": "lib/src/profile/profile_providers.dart", "line_start": 116, "line_end": 120,
     "label": "FP", "category": "business", "severity": 0, "summary": "RunHistoryStore.add replaces same-seed entry",
     "signals": ["duplicate", "twice", "two entries", "recent runs"]},
    {"id": "ads-show-timeout", "path": "lib/src/ads/ads_service.dart", "line_start": 135, "line_end": 167,
     "label": "MISSED", "category": "correctness", "severity": 4, "summary": "show path has no timeout; sheet soft-locks",
     "signals": ["timeout", "never", "stuck", "soft-lock", "pending forever"]},
    {"id": "control-record-run", "path": "lib/src/profile/profile_providers.dart", "line_start": 85, "line_end": 112,
     "label": "CONTROL", "category": "correctness", "severity": 0, "summary": "recordRun baseline fold is idempotent",
     "signals": ["idempot", "double", "persist twice"]}
  ]
}
```

Rules while transcribing: `severity` 4 = data/money loss or lock-up, 3 = user-visible bug, 2 = latent bug or design debt, 1 = hygiene; negatives get 0. Signals are lowercase substrings about the *defect*, not the fix wording.

- [ ] **Step 6: Add the diff fetch script and CLI flags**

```bash
# tests/eval/fetch_pr_diff.sh
#!/usr/bin/env bash
# Usage: tests/eval/fetch_pr_diff.sh owner/repo PR_NUMBER out.diff
set -euo pipefail
gh pr diff "$2" --repo "$1" > "$3"
echo "wrote $(wc -l < "$3") lines to $3"
```

In `tests/eval/run_eval.py`, after the existing `add_argument` calls (line 220) add:

```python
    parser.add_argument("--labels", help="path to a labeled real-PR JSON (tests/eval/labels/*.json)")
    parser.add_argument("--diff-file", help="unified diff for --labels; fetch with tests/eval/fetch_pr_diff.sh")
```

and in the main flow, before mutant/curated runs:

```python
    if args.labels:
        from tests.eval.labels import load_labels, score_labels
        if not args.diff_file:
            parser.error("--labels requires --diff-file")
        label_set = load_labels(args.labels)
        diff_text = Path(args.diff_file).read_text()
        review = run_review_on_diff(diff_text)  # existing helper that drives PRReviewer via PlainDiffGitProvider
        report = score_labels(label_set, review)
        print(json.dumps(report.as_dict(), indent=2))
        if args.out:
            Path(args.out).write_text(json.dumps({"labels": report.as_dict()}, indent=2))
        return
```

If `run_review_on_diff` does not exist under that name, use the harness's existing function that builds `PlainDiffGitProvider(diff_text)` and awaits `PRReviewer(...).run()`; do not duplicate it.

- [ ] **Step 7: Document and commit**

Add to `tests/eval/README.md`:

```markdown
## Labeled real PRs
tests/eval/fetch_pr_diff.sh samer2373/block_rush 1 /tmp/block_rush_pr1.diff
PYTHONPATH=. uv run python tests/eval/run_eval.py --labels tests/eval/labels/block_rush_pr1.json --diff-file /tmp/block_rush_pr1.diff --out /tmp/labels.json
Reports precision, recall, severity-weighted recall, control false flags. Labels come from the 2026-09-10 audit.
```

```bash
chmod +x tests/eval/fetch_pr_diff.sh
uv run ruff check --fix tests/eval/labels.py tests/eval/run_eval.py tests/unittest/test_eval_labels.py
git add tests/eval/labels.py tests/eval/labels/block_rush_pr1.json tests/eval/fetch_pr_diff.sh tests/eval/run_eval.py tests/eval/README.md tests/unittest/test_eval_labels.py
git commit -m "test(eval): add labeled real-PR corpus and scorer from block_rush audit

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Per-call ledger with stage attribution (R-2)

**Files:**
- Create: `pr_agent/algo/run_ledger.py`
- Modify: `pr_agent/algo/run_details.py:22-60` (add `calls` list), `:146-160` (`record_ai_call` signature)
- Modify: `pr_agent/algo/ai_handlers/litellm_ai_handler.py:775` (`chat_completion` gains `stage`, `files`), `:477` (`_record_completion_metadata` passes them)
- Modify: `pr_agent/tools/pr_reviewer.py:~1004` (`_get_prediction` passes `stage`), `:848-930` (chunk index)
- Modify: `pr_agent/settings/configuration.toml` `[config]` (add `run_ledger_path = ""`)
- Test: `tests/unittest/test_run_ledger.py`

**Interfaces:**
- Produces: `CallRecord` dataclass (`run_id, tool, stage, chunk_index, sample_index, files, model, prompt_tokens, cached_tokens, completion_tokens, cost_usd, latency_ms, findings_emitted`); `record_ai_call(usage=None, model=None, cost_usd=None, *, stage=None, chunk_index=None, sample_index=None, files=None, latency_ms=None)`; `RunDetails.calls: list[CallRecord]`; `write_ledger(details: RunDetails, path: str, run_id: str, tool: str) -> int` (rows written).
- Consumes: `get_run_details()` from `run_details.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unittest/test_run_ledger.py
import json

from pr_agent.algo.run_details import RunDetails, install_run_details, record_ai_call
from pr_agent.algo.run_ledger import write_ledger


class _Usage:
    prompt_tokens = 100
    completion_tokens = 20
    total_tokens = 120
    prompt_tokens_details = type("D", (), {"cached_tokens": 60})()


def test_record_ai_call_appends_call_record():
    details = RunDetails()
    with install_run_details(details):
        record_ai_call(_Usage(), model="m", cost_usd="0.01", stage="review", chunk_index=2,
                       sample_index=0, files=["a.py", "b.py"], latency_ms=812)
    assert len(details.calls) == 1
    call = details.calls[0]
    assert (call.stage, call.chunk_index, call.files) == ("review", 2, ("a.py", "b.py"))
    assert (call.prompt_tokens, call.cached_tokens, call.completion_tokens) == (100, 60, 20)
    assert details.total_tokens == 120


def test_write_ledger_emits_one_jsonl_row_per_call(tmp_path):
    details = RunDetails()
    with install_run_details(details):
        record_ai_call(_Usage(), model="m", stage="review", chunk_index=0)
        record_ai_call(_Usage(), model="m", stage="verify", chunk_index=None)
    path = tmp_path / "ledger.jsonl"
    rows = write_ledger(details, str(path), run_id="r1", tool="review")
    lines = path.read_text().splitlines()
    assert rows == 2 and len(lines) == 2
    first = json.loads(lines[0])
    assert first["run_id"] == "r1" and first["stage"] == "review" and first["prompt_tokens"] == 100
    assert sum(json.loads(l)["prompt_tokens"] + json.loads(l)["completion_tokens"] for l in lines) == details.total_tokens
```

If `install_run_details` is not the real context-manager name in `run_details.py`, use the one the existing `tests/unittest/test_run_details.py` uses.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_run_ledger.py -q`
Expected: FAIL with `ImportError` for `run_ledger` / unexpected keyword `stage`

- [ ] **Step 3: Implement**

In `pr_agent/algo/run_details.py`, add above `RunDetails`:

```python
@dataclass(frozen=True)
class CallRecord:
    stage: str
    model: str
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Optional[Decimal] = None
    chunk_index: Optional[int] = None
    sample_index: Optional[int] = None
    files: tuple[str, ...] = ()
    latency_ms: Optional[int] = None
    findings_emitted: Optional[int] = None
```

Add to `RunDetails` fields: `calls: list[CallRecord] = field(default_factory=list)`.

Replace `record_ai_call`:

```python
def record_ai_call(usage=None, model: Optional[str] = None, cost_usd=None, *,
                   stage: Optional[str] = None, chunk_index: Optional[int] = None,
                   sample_index: Optional[int] = None, files=None, latency_ms: Optional[int] = None,
                   findings_emitted: Optional[int] = None) -> None:
    """Count one successful AI call, accumulate usage and known cost, and keep a per-call record."""
    details = get_run_details()
    if details is None:
        return
    details.num_ai_calls += 1
    if usage is not None:
        add_token_usage(usage)
    cost = _as_decimal_cost(cost_usd)
    if cost is not None:
        details.total_cost_usd += cost
        details.known_cost_call_count += 1
        model_name = model or "unknown"
        details.model_costs_usd[model_name] = details.model_costs_usd.get(model_name, Decimal("0")) + cost
    details.calls.append(CallRecord(
        stage=stage or "unknown", model=model or "unknown",
        prompt_tokens=_usage_int(usage, "prompt_tokens"),
        cached_tokens=_cached_tokens(usage),
        completion_tokens=_usage_int(usage, "completion_tokens"),
        cost_usd=cost, chunk_index=chunk_index, sample_index=sample_index,
        files=tuple(files or ()), latency_ms=latency_ms, findings_emitted=findings_emitted))


def _usage_int(usage, name: str) -> int:
    value = getattr(usage, name, None) if usage is not None else None
    if value is None and isinstance(usage, dict):
        value = usage.get(name)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _cached_tokens(usage) -> int:
    details = getattr(usage, "prompt_tokens_details", None) if usage is not None else None
    if details is None and isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
    return _usage_int(details, "cached_tokens") if details is not None else 0
```

Create `pr_agent/algo/run_ledger.py`:

```python
"""Append one JSON line per model call so tokens are attributable to a stage and a file set."""
from __future__ import annotations

import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from pr_agent.algo.run_details import RunDetails


def write_ledger(details: RunDetails, path: str, *, run_id: str, tool: str) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with target.open("a", encoding="utf-8") as fh:
        for call in details.calls:
            row = asdict(call)
            row["files"] = list(call.files)
            row["cost_usd"] = str(call.cost_usd) if isinstance(call.cost_usd, Decimal) else None
            row.update(run_id=run_id, tool=tool)
            fh.write(json.dumps(row, sort_keys=True) + "\n")
            rows += 1
    return rows
```

In `litellm_ai_handler.py`: add `stage: str = None, files=None` keyword params to `chat_completion` (line 775) and pass them plus a measured `latency_ms` (wrap the completion call with `time.monotonic()`) into `_record_completion_metadata(..., stage=stage, files=files, latency_ms=latency_ms)`, which forwards to `record_ai_call`. Keep the positional signature unchanged so other callers are unaffected.

In `pr_reviewer.py` `_get_prediction`, pass `stage="review"` and, when called from `_prepare_chunked_prediction`, `chunk_index` (thread it through `_get_review_data(model, patches_diff, chunk_index=None)`); `sample_index` when `num_samples > 1`. Add `files` = the chunk's files once Task 3 provides them (leave `None` for now).

In `PRReviewer.run()` after publishing, when `get_settings().config.get("run_ledger_path")`: call `write_ledger(get_run_details(), path, run_id=<review run id already used for state>, tool="review")` inside a `try/except Exception` that logs and continues.

`configuration.toml` `[config]`:

```toml
# When set, append one JSON line per model call (stage, chunk, files, tokens, cost, latency) to this file.
run_ledger_path = ""
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_run_ledger.py tests/unittest/test_run_details.py tests/unittest/test_litellm_run_details.py tests/unittest/test_run_details_wiring.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix pr_agent/algo/run_details.py pr_agent/algo/run_ledger.py pr_agent/algo/ai_handlers/litellm_ai_handler.py pr_agent/tools/pr_reviewer.py tests/unittest/test_run_ledger.py
git add -A pr_agent/algo/run_details.py pr_agent/algo/run_ledger.py pr_agent/algo/ai_handlers/litellm_ai_handler.py pr_agent/tools/pr_reviewer.py pr_agent/settings/configuration.toml tests/unittest/test_run_ledger.py
git commit -m "feat(telemetry): record per-call stage, chunk, files and tokens; optional JSONL ledger

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Line-level coverage ledger (R-3)

**Files:**
- Create: `pr_agent/algo/review_coverage.py`
- Modify: `pr_agent/algo/pr_processing.py:397-540` (new `get_pr_multi_diffs_with_files`; existing function delegates)
- Modify: `pr_agent/tools/pr_reviewer.py:853-860` (use new function), `:1086-1140` (footer)
- Test: `tests/unittest/test_review_coverage.py`

**Interfaces:**
- Produces: `FileCoverage(path, changed_lines, status)` with `status ∈ {"reviewed","clipped","skipped_budget","chunk_failed","deletion_only","low_priority_summary","ignored"}`; `CoverageLedger` with `.files: dict[str, FileCoverage]`, `.mark(path, status)`, `.reviewed_ratio -> float`, `.render_footer() -> str`, `.not_fully_reviewed() -> list[str]`; `ChunkPlan(diff: str, files: tuple[str, ...], clipped: tuple[str, ...])`; `get_pr_multi_diffs_with_files(git_provider, token_handler, model, max_calls, add_line_numbers=True) -> tuple[list[ChunkPlan], list[str]]`.
- Consumes: `FilePatchInfo.filename`, `.patch`, `.num_plus_lines`, `.num_minus_lines` from `pr_agent/algo/types.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unittest/test_review_coverage.py
from pr_agent.algo.review_coverage import CoverageLedger, FileCoverage


def _ledger():
    ledger = CoverageLedger()
    ledger.add(FileCoverage("a.py", changed_lines=100, status="reviewed"))
    ledger.add(FileCoverage("b.py", changed_lines=100, status="clipped"))
    ledger.add(FileCoverage("c.py", changed_lines=200, status="chunk_failed"))
    ledger.add(FileCoverage("d.py", changed_lines=0, status="deletion_only"))
    return ledger


def test_clipped_counts_half_and_failed_counts_zero():
    ledger = _ledger()
    # reviewed 100 + clipped 50 (half credit) + failed 0 over 400 changed lines
    assert ledger.reviewed_ratio == 0.375


def test_not_fully_reviewed_lists_partial_and_failed_only():
    assert _ledger().not_fully_reviewed() == ["b.py", "c.py"]


def test_footer_names_percentage_and_count():
    footer = _ledger().render_footer()
    assert "Reviewed 38% of changed lines" in footer
    assert "2 file(s) not fully reviewed" in footer


def test_mark_overrides_status():
    ledger = _ledger()
    ledger.mark("a.py", "chunk_failed")
    assert ledger.files["a.py"].status == "chunk_failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_review_coverage.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement the module**

```python
# pr_agent/algo/review_coverage.py
"""Per-file, line-weighted record of what a review actually looked at.

A file the model saw whole is reviewed. A clipped patch earns half credit: the model saw some
of it and we cannot say which half mattered. A failed chunk, a budget skip and an ignored file
earn nothing. Deletion-only files carry no changed lines to review.
"""
from __future__ import annotations

from dataclasses import dataclass, field

STATUS_CREDIT = {
    "reviewed": 1.0,
    "clipped": 0.5,
    "low_priority_summary": 0.0,
    "skipped_budget": 0.0,
    "chunk_failed": 0.0,
    "ignored": 0.0,
    "deletion_only": 0.0,
}
FULL_STATUSES = {"reviewed", "deletion_only", "ignored"}


@dataclass
class FileCoverage:
    path: str
    changed_lines: int
    status: str

    def __post_init__(self) -> None:
        if self.status not in STATUS_CREDIT:
            raise ValueError(f"unknown coverage status {self.status!r}")


@dataclass
class CoverageLedger:
    files: dict[str, FileCoverage] = field(default_factory=dict)

    def add(self, entry: FileCoverage) -> None:
        self.files[entry.path] = entry

    def mark(self, path: str, status: str) -> None:
        entry = self.files.get(path)
        if entry is None:
            self.files[path] = FileCoverage(path, changed_lines=0, status=status)
        else:
            entry.status = status if status in STATUS_CREDIT else entry.status

    @property
    def reviewed_ratio(self) -> float:
        total = sum(f.changed_lines for f in self.files.values())
        if total == 0:
            return 1.0
        credit = sum(f.changed_lines * STATUS_CREDIT[f.status] for f in self.files.values())
        return credit / total

    def not_fully_reviewed(self) -> list[str]:
        return sorted(p for p, f in self.files.items() if f.status not in FULL_STATUSES)

    def render_footer(self) -> str:
        pct = round(self.reviewed_ratio * 100)
        partial = self.not_fully_reviewed()
        line = f"Reviewed {pct}% of changed lines"
        if partial:
            line += f" · {len(partial)} file(s) not fully reviewed"
        return line
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_review_coverage.py -q`
Expected: PASS

- [ ] **Step 5: Add chunk-plan output to pr_processing**

In `pr_agent/algo/pr_processing.py`, add after `get_pr_multi_diffs`:

```python
@dataclass(frozen=True)
class ChunkPlan:
    diff: str
    files: tuple[str, ...]
    clipped: tuple[str, ...]


def get_pr_multi_diffs_with_files(git_provider, token_handler, model: str, max_calls: int = 5,
                                  add_line_numbers: bool = True) -> tuple[list[ChunkPlan], list[str]]:
    """Same chunking as get_pr_multi_diffs, but each chunk also names its files and which were clipped."""
```

Implement by refactoring the body of `get_pr_multi_diffs` so the loop that builds `patches` also appends to `chunk_files: list[str]` and `chunk_clipped: list[str]` (in the `large_patch_policy == 'clip'` branch, ~line 482, append the filename to `chunk_clipped`), and flushes `ChunkPlan(diff, tuple(chunk_files), tuple(chunk_clipped))` wherever a chunk string is finalized. Then make `get_pr_multi_diffs` call the new function and return `[plan.diff for plan in plans]` (plus `remaining_files` when `return_remaining_files=True`) so existing callers and tests are byte-identical. Add a unit test in `tests/unittest/test_pr_processing_chunks.py` (create) with two fake `FilePatchInfo` objects and a token handler that forces two chunks, asserting `plans[0].files == ("a.py",)` and that a patch over budget under `large_patch_policy='clip'` appears in `.clipped`.

- [ ] **Step 6: Wire into the reviewer**

In `_prepare_chunked_prediction` (`pr_reviewer.py:853`), switch to `get_pr_multi_diffs_with_files`, keep `patches_diff_list = [p.diff for p in plans]`, store `self.chunk_plans = plans`. Build `self.coverage = CoverageLedger()` from `self.git_provider.get_diff_files()`: `changed_lines = file.num_plus_lines + file.num_minus_lines`, status `deletion_only` when `num_plus_lines == 0 and num_minus_lines > 0`, else `reviewed`. Then mark: every path in `plan.clipped` → `clipped`; every path in `remaining_files_list` → `skipped_budget`; after the retry loop, every file of a chunk index not in `chunk_results` → `chunk_failed`. For the single-call path, build the same ledger with `remaining_files` from `get_pr_diff(..., return_remaining_files=True)`.

In `_prepare_pr_review`, where the coverage footer is currently appended (search `enable_review_coverage_footer`), append `self.coverage.render_footer()` when the ledger exists. When `self.coverage.reviewed_ratio < 0.95`, prepend (not append) this block right after the heading:

```python
warning = (f"> ⚠️ **Partial review.** {self.coverage.render_footer()}. "
           "Findings below cover only the reviewed lines; a follow-up run is needed.")
```

Also pass `files=list(plan.files)` into `_get_review_data` → `chat_completion(..., files=...)` from Task 2.

- [ ] **Step 7: Run the reviewer and processing tests**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_review_coverage.py tests/unittest/test_pr_processing_chunks.py tests/unittest/test_pr_reviewer_core.py tests/unittest/test_review_consensus_partiality.py -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
uv run ruff check --fix pr_agent/algo/review_coverage.py pr_agent/algo/pr_processing.py pr_agent/tools/pr_reviewer.py tests/unittest/test_review_coverage.py tests/unittest/test_pr_processing_chunks.py
git add -A pr_agent/algo/review_coverage.py pr_agent/algo/pr_processing.py pr_agent/tools/pr_reviewer.py tests/unittest/test_review_coverage.py tests/unittest/test_pr_processing_chunks.py
git commit -m "feat(review): line-weighted coverage ledger; clipped and failed files no longer count as reviewed

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Safe cross-run identity (R-6)

**Files:**
- Modify: `pr_agent/algo/review_merge.py:581` (add `same_finding_across_runs`)
- Modify: `pr_agent/algo/review_finding_state.py:189-260` (match by identity function, not id equality)
- Test: `tests/unittest/test_review_cross_run_identity.py`

**Interfaces:**
- Produces: `same_finding_across_runs(a: Mapping, b: Mapping) -> bool` (both in state-record shape: `path, body, line_start, line_end`); `normalized_header(body: str) -> str` (first bold segment, lowercased, punctuation stripped).
- Consumes: `line_ranges_overlap`, `_similar_wording` semantics (Jaccard ≥ `VOTE_TEXT_SIMILARITY` with ≥ `VOTE_MIN_DISTINCTIVE_WORDS`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unittest/test_review_cross_run_identity.py
from pr_agent.algo.review_finding_state import reconcile_review_findings
from pr_agent.algo.review_merge import same_finding_across_runs

DEBUG_A = {"path": "test/shell/themes_screen_test.dart", "line_start": 67, "line_end": 87,
           "body": "**Debug Leftovers**\n\nThe `tapSet` helper still calls `print(...)` and `debugDumpApp()` on every scroll-and-tap."}
DEBUG_B = {"path": "test/shell/themes_screen_test.dart", "line_start": 67, "line_end": 88,
           "body": "**Leftover Debug Code**\n\n`tapSet` contains leftover debugging statements including `debugDumpApp()` and multiple `print` calls."}
OTHER_NEARBY = {"path": "test/shell/themes_screen_test.dart", "line_start": 70, "line_end": 72,
                "body": "**Flaky Wait**\n\nThe test awaits a fixed Duration instead of pumpAndSettle, which is timing dependent."}


def test_reworded_same_defect_matches():
    assert same_finding_across_runs(DEBUG_A, DEBUG_B)


def test_nearby_distinct_defect_does_not_match():
    assert not same_finding_across_runs(DEBUG_A, OTHER_NEARBY)


def test_same_wording_moved_far_does_not_match():
    moved = dict(DEBUG_A, line_start=400, line_end=420)
    assert not same_finding_across_runs(DEBUG_A, moved)


def test_reconcile_keeps_one_record_for_reworded_finding():
    first = reconcile_review_findings(None, [DEBUG_A], allow_resolution=False, head_sha="s1", run_id="r1")
    second = reconcile_review_findings(first.state, [DEBUG_B], allow_resolution=False, head_sha="s2", run_id="r2")
    active = [f for f in second.state["findings"] if f["state"] == "ACTIVE"]
    assert len(active) == 1
    assert active[0]["finding_id"] == first.state["findings"][0]["finding_id"]
    assert "Leftover Debug Code" in active[0]["body"]  # latest wording kept, first id kept
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_review_cross_run_identity.py -q`
Expected: FAIL with `ImportError: cannot import name 'same_finding_across_runs'`

- [ ] **Step 3: Implement**

In `review_merge.py` after `_same_finding`:

```python
_HEADER_RE = re.compile(r"\*\*(.+?)\*\*")


def normalized_header(body: str) -> str:
    match = _HEADER_RE.search(body or "")
    text = match.group(1) if match else (body or "").splitlines()[0] if body else ""
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def _state_range(record: Mapping) -> Optional[tuple[int, int]]:
    start, end = record.get("line_start"), record.get("line_end")
    if start is None:
        return None
    return int(start), int(end if end is not None else start)


def same_finding_across_runs(a: Mapping, b: Mapping) -> bool:
    """Stricter than _same_finding: across commits, overlap alone would merge neighbours.

    Same path, overlapping lines, and either the same normalized header or similar wording.
    """
    if normalize_finding_path(a.get("path")) != normalize_finding_path(b.get("path")):
        return False
    ra, rb = _state_range(a), _state_range(b)
    if ra is None or rb is None or not line_ranges_overlap(ra, rb):
        return False
    if normalized_header(a.get("body", "")) and normalized_header(a.get("body", "")) == normalized_header(b.get("body", "")):
        return True
    return _similar_wording({"issue_content": a.get("body", "")}, {"issue_content": b.get("body", "")})
```

Check `_similar_wording`'s input shape at `review_merge.py:565`; if it reads `issue_header`/`issue_content`, pass both from the body split on the first blank line.

In `review_finding_state.py::reconcile_review_findings`, replace the id-only lookup. After `previous_by_id` / `current_by_id` are built:

```python
    from pr_agent.algo.review_merge import same_finding_across_runs

    def _previous_match(current_finding):
        exact = previous_by_id.get(current_finding["finding_id"])
        if exact is not None:
            return exact
        for candidate in previous_findings:
            if candidate.get("state") != "RESOLVED" and same_finding_across_runs(candidate, current_finding):
                return candidate
        return None
```

Then in the loop over `current_by_id`, use `previous = _previous_match(current_finding)`; when a match exists with a different id, keep `record["finding_id"] = previous["finding_id"]`, update body/lines to the current ones, and add `previous["finding_id"]` to a `matched_previous_ids` set that the second loop (absent findings) skips instead of `finding_id in current_by_id`. Key `reconciled` by the retained id.

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_review_cross_run_identity.py tests/unittest/test_review_finding_state.py tests/unittest/test_pr_reviewer_finding_state.py tests/unittest/test_review_consensus_sampling.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix pr_agent/algo/review_merge.py pr_agent/algo/review_finding_state.py tests/unittest/test_review_cross_run_identity.py
git add pr_agent/algo/review_merge.py pr_agent/algo/review_finding_state.py tests/unittest/test_review_cross_run_identity.py
git commit -m "fix(review): match reworded findings across runs by path, overlap and header, not body hash

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Evidence-based resolution with `UNCONFIRMED` (R-5)

**Files:**
- Modify: `pr_agent/algo/review_finding_state.py:111-135` (schema v2 validation accepts v1), `:189-260` (`fully_reviewed_files` param), `:289` (render)
- Modify: `pr_agent/tools/pr_reviewer.py:703-770` (pass `fully_reviewed_files` from the coverage ledger)
- Test: `tests/unittest/test_review_resolution_evidence.py`

**Interfaces:**
- Produces: `reconcile_review_findings(..., fully_reviewed_files: Iterable[str] | None = None)`; state `STATE_SCHEMA_VERSION = 2`; finding `state ∈ {ACTIVE, UNCONFIRMED, RESOLVED}`; `unconfirmed_at` field.
- Consumes: `CoverageLedger.files` statuses from Task 3.

- [ ] **Step 1: Write the failing test**

```python
# tests/unittest/test_review_resolution_evidence.py
from pr_agent.algo.review_finding_state import parse_review_state, reconcile_review_findings, serialize_review_state

F = {"path": "lib/a.dart", "line_start": 10, "line_end": 12, "body": "**Bug**\n\nnull deref on empty list"}


def _first():
    return reconcile_review_findings(None, [F], allow_resolution=False, head_sha="s1", run_id="r1").state


def test_absent_finding_in_unreviewed_file_becomes_unconfirmed():
    result = reconcile_review_findings(_first(), [], allow_resolution=True, head_sha="s2", run_id="r2",
                                       fully_reviewed_files=["lib/other.dart"])
    assert result.state["findings"][0]["state"] == "UNCONFIRMED"
    assert result.resolved_ids == ()


def test_absent_finding_in_fully_reviewed_file_resolves():
    result = reconcile_review_findings(_first(), [], allow_resolution=True, head_sha="s2", run_id="r2",
                                       fully_reviewed_files=["lib/a.dart"])
    assert result.state["findings"][0]["state"] == "RESOLVED"
    assert result.resolved_ids != ()


def test_no_reviewed_files_given_never_resolves():
    result = reconcile_review_findings(_first(), [], allow_resolution=True, head_sha="s2", run_id="r2")
    assert result.state["findings"][0]["state"] == "UNCONFIRMED"


def test_unconfirmed_reemitted_returns_to_active():
    unconfirmed = reconcile_review_findings(_first(), [], allow_resolution=True, head_sha="s2", run_id="r2").state
    result = reconcile_review_findings(unconfirmed, [F], allow_resolution=False, head_sha="s3", run_id="r3")
    assert result.state["findings"][0]["state"] == "ACTIVE"
    assert result.reopened_ids == ()  # UNCONFIRMED -> ACTIVE is not a reopen


def test_v1_marker_still_parses_and_upgrades():
    v1 = dict(_first(), schema_version=1)
    body = "review\n\n" + serialize_review_state(v1)
    parsed = parse_review_state(body)
    assert parsed.state is not None and parsed.state["schema_version"] == 1
    result = reconcile_review_findings(parsed.state, [F], allow_resolution=False, head_sha="s2", run_id="r2")
    assert result.state["schema_version"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_review_resolution_evidence.py -q`
Expected: FAIL (`unexpected keyword argument 'fully_reviewed_files'`, state RESOLVED where UNCONFIRMED expected)

- [ ] **Step 3: Implement**

In `review_finding_state.py`: set `STATE_SCHEMA_VERSION = 2`; in `_is_valid_state` accept `schema_version in (1, 2)` and `state in {"ACTIVE", "UNCONFIRMED", "RESOLVED"}`. Add the parameter and change the absent-finding loop:

```python
    reviewed_paths = {normalize_finding_path(p) for p in (fully_reviewed_files or []) if p}
    ...
    for finding_id, previous in previous_by_id.items():
        if finding_id in matched_previous_ids:
            continue
        record = copy.deepcopy(previous)
        state_now = record.get("state")
        file_reviewed = normalize_finding_path(record.get("path")) in reviewed_paths
        if state_now in ("ACTIVE", "UNCONFIRMED") and resolution_allowed and file_reviewed:
            record["state"] = "RESOLVED"
            record["resolved_at"] = now
            if head_sha:
                record["resolved_head_sha"] = head_sha
            if run_id:
                record["resolution_run_id"] = run_id
            resolved_ids.append(finding_id)
            changed = True
        elif state_now == "ACTIVE":
            record["state"] = "UNCONFIRMED"
            record["unconfirmed_at"] = now
            changed = True
        reconciled[finding_id] = record
```

In the present-finding loop, treat `old_state == "UNCONFIRMED"` as a plain return to ACTIVE (no `reopened_*` bump); only `RESOLVED → ACTIVE` counts as reopen. Import `normalize_finding_path` from `review_merge`.

In `pr_reviewer.py::_prepare_review_finding_state`, compute:

```python
        fully_reviewed = [p for p, f in getattr(self, "coverage", CoverageLedger()).files.items()
                          if f.status == "reviewed"]
```

and pass `fully_reviewed_files=fully_reviewed` to both `reconcile_review_findings` calls (lines 761, 767). Keep the existing `allow_resolution` guards unchanged; this narrows resolution further, never widens it.

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_review_resolution_evidence.py tests/unittest/test_review_finding_state.py tests/unittest/test_pr_reviewer_finding_state.py tests/unittest/test_review_cross_run_identity.py -q`
Expected: PASS. If an existing test asserts RESOLVED without providing reviewed files, update that test to pass `fully_reviewed_files=[path]`, since the old behavior is the defect being fixed.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix pr_agent/algo/review_finding_state.py pr_agent/tools/pr_reviewer.py tests/unittest/test_review_resolution_evidence.py
git add pr_agent/algo/review_finding_state.py pr_agent/tools/pr_reviewer.py tests/unittest/test_review_resolution_evidence.py
git commit -m "fix(review): resolve findings only when their file was fully re-reviewed; add UNCONFIRMED state

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Render every ACTIVE finding, human text first (R-4)

**Files:**
- Modify: `pr_agent/algo/review_finding_state.py:289-360` (`render_carried_section`, budget order in `append_review_state`)
- Modify: `pr_agent/tools/pr_reviewer.py:1086-1156` (call carried renderer with current ids)
- Test: `tests/unittest/test_review_carried_rendering.py`

**Interfaces:**
- Produces: `render_carried_section(state, current_ids: set[str], fully_reviewed_files: Iterable[str]) -> str`; `append_review_state(review_body, state, max_chars=None, *, carried_section: str = "")` — budget order: human body, carried section, marker; when over budget, drop RESOLVED findings from the marker first, then truncate carried, then truncate body.

- [ ] **Step 1: Write the failing test**

```python
# tests/unittest/test_review_carried_rendering.py
from pr_agent.algo.review_finding_state import (append_review_state, parse_review_state,
                                                reconcile_review_findings, render_carried_section)

A = {"path": "lib/a.dart", "line_start": 1, "line_end": 2, "body": "**A**\n\nfirst issue"}
B = {"path": "lib/b.dart", "line_start": 5, "line_end": 6, "body": "**B**\n\nsecond issue"}


def _state_with_two_then_one():
    s1 = reconcile_review_findings(None, [A, B], allow_resolution=False, head_sha="s1", run_id="r1").state
    return reconcile_review_findings(s1, [A], allow_resolution=False, head_sha="s2", run_id="r2",
                                     fully_reviewed_files=["lib/a.dart"])


def test_carried_section_lists_findings_not_in_current_run():
    result = _state_with_two_then_one()
    current_ids = {f["finding_id"] for f in result.state["findings"] if f["path"] == "lib/a.dart"}
    section = render_carried_section(result.state, current_ids, fully_reviewed_files=["lib/a.dart"])
    assert "Carried from earlier runs" in section
    assert "lib/b.dart" in section and "not re-reviewed this run" in section
    assert "lib/a.dart" not in section


def test_visible_count_equals_active_plus_unconfirmed():
    result = _state_with_two_then_one()
    section = render_carried_section(result.state, set(), fully_reviewed_files=[])
    assert section.count("- **") == 2


def test_budget_drops_resolved_before_truncating_human_text():
    s1 = reconcile_review_findings(None, [A, B], allow_resolution=False, head_sha="s1", run_id="r1").state
    s2 = reconcile_review_findings(s1, [A], allow_resolution=True, head_sha="s2", run_id="r2",
                                   fully_reviewed_files=["lib/a.dart", "lib/b.dart"]).state
    assert any(f["state"] == "RESOLVED" for f in s2["findings"])
    body = "x" * 400
    out = append_review_state(body, s2, max_chars=len(body) + 60 + len(__import__("json").dumps(s2)))
    parsed = parse_review_state(out)
    assert out.startswith(body)  # human text intact
    assert all(f["state"] != "RESOLVED" for f in parsed.state["findings"])  # resolved dropped first
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_review_carried_rendering.py -q`
Expected: FAIL (`render_carried_section` missing; human text truncated)

- [ ] **Step 3: Implement**

```python
def render_carried_section(state: Mapping[str, Any], current_ids: set[str],
                           fully_reviewed_files: Iterable[str]) -> str:
    reviewed = {normalize_finding_path(p) for p in fully_reviewed_files}
    carried = [f for f in state.get("findings", [])
               if f.get("state") in ("ACTIVE", "UNCONFIRMED") and f.get("finding_id") not in current_ids]
    if not carried:
        return ""
    lines = ["### Carried from earlier runs", ""]
    for f in carried:
        path = f.get("path", "")
        loc = f"{path}:{f['line_start']}" if f.get("line_start") else path
        note = "re-reviewed, not re-emitted" if normalize_finding_path(path) in reviewed else "not re-reviewed this run"
        tag = " · unconfirmed" if f.get("state") == "UNCONFIRMED" else ""
        header = f.get("body", "").split("\n", 1)[0].strip("* ")
        lines.append(f"- **{header}** — `{loc}` · first seen {f.get('first_seen', '')[:10]} · {note}{tag}")
    return "\n".join(lines)
```

Change `append_review_state` to accept `carried_section: str = ""` and compose `human_body` from `(body, carried_section, _render_resolved_section(state))`. When `max_chars` is given and the total exceeds it: first rebuild `marker` from a copy of `state` with RESOLVED findings removed (`_retained_findings(..., 0)`), then, if still over, truncate `carried_section` to fit, then the body as today. Raise `ValueError` only if the marker without RESOLVED entries still does not fit.

In `pr_reviewer.py::_prepare_pr_review` around lines 1140/1156: compute `current_ids = {key_issue_fingerprint(...)}` for this run's findings (the same ids `reconcile` assigned; reuse `state_result.state` and pick findings whose `last_seen_head_sha == head_sha and last_seen == this run's timestamp`, or simpler: keep the set returned by normalizing the current findings), then `carried = render_carried_section(state_result.state, current_ids, fully_reviewed)` and pass `carried_section=carried` into `append_review_state`. Make sure the fallback path that re-appends the previous state on `ValueError` also passes the carried section.

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_review_carried_rendering.py tests/unittest/test_review_finding_state.py tests/unittest/test_pr_reviewer_finding_state.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix pr_agent/algo/review_finding_state.py pr_agent/tools/pr_reviewer.py tests/unittest/test_review_carried_rendering.py
git add pr_agent/algo/review_finding_state.py pr_agent/tools/pr_reviewer.py tests/unittest/test_review_carried_rendering.py
git commit -m "fix(review): render carried findings from state; human text gets the byte budget before the marker

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Bounded recovery — split, fallback model, then fail (R-7)

**Files:**
- Modify: `pr_agent/tools/pr_reviewer.py:848-930`
- Modify: `pr_agent/settings/configuration.toml` `[pr_reviewer]` (`chunk_split_on_failure = true`, `chunk_fallback_model_on_failure = true`)
- Test: `tests/unittest/test_review_chunk_recovery.py`

**Interfaces:**
- Consumes: `ChunkPlan` (Task 3), `CHUNK_REVIEW_ATTEMPTS`, `self._get_review_data(model, patches_diff, chunk_index=None, files=None)`.
- Produces: `split_chunk_plan(plan: ChunkPlan, git_provider, token_handler, model) -> list[ChunkPlan]` (module-level helper in `pr_reviewer.py`; returns two plans by splitting `plan.files` in half and regenerating diffs via `get_pr_multi_diffs_with_files` over just those files, or `[plan]` when it has one file).

- [ ] **Step 1: Write the failing test**

```python
# tests/unittest/test_review_chunk_recovery.py
import pytest

from pr_agent.algo.pr_processing import ChunkPlan
from pr_agent.tools import pr_reviewer as mod


class _Reviewer(mod.PRReviewer):
    """Bypass __init__; exercise only the chunk loop."""
    def __init__(self, plans, failures):
        self.chunk_plans = plans
        self._failures = failures  # set of diff strings that fail on the primary model
        self.calls = []
        self.coverage = mod.CoverageLedger()
        for p in plans:
            for f in p.files:
                self.coverage.add(mod.FileCoverage(f, 10, "reviewed"))

    async def _get_review_data(self, model, patches_diff=None, chunk_index=None, files=None):
        self.calls.append((model, patches_diff))
        if patches_diff in self._failures and model == "primary":
            raise RuntimeError("boom")
        return ("raw", {"review": {"key_issues_to_review": []}}, 0)


@pytest.mark.asyncio
async def test_failed_chunk_is_split_then_reviewed(monkeypatch):
    plans = [ChunkPlan("AB", ("a.py", "b.py"), ()), ChunkPlan("C", ("c.py",), ())]
    monkeypatch.setattr(mod, "split_chunk_plan", lambda plan, *a: [ChunkPlan("A", ("a.py",), ()), ChunkPlan("B", ("b.py",), ())])
    monkeypatch.setattr(mod, "CHUNK_REVIEW_ATTEMPTS", 1)
    r = _Reviewer(plans, failures={"AB"})
    ok = await r._review_chunk_plans("primary", fallback_models=["secondary"])
    assert ok
    assert r.coverage.reviewed_ratio == 1.0
    assert ("primary", "A") in r.calls and ("primary", "B") in r.calls


@pytest.mark.asyncio
async def test_unsplittable_chunk_falls_back_to_secondary_model(monkeypatch):
    plans = [ChunkPlan("A", ("a.py",), ())]
    monkeypatch.setattr(mod, "split_chunk_plan", lambda plan, *a: [plan])
    monkeypatch.setattr(mod, "CHUNK_REVIEW_ATTEMPTS", 1)
    r = _Reviewer(plans, failures={"A"})
    ok = await r._review_chunk_plans("primary", fallback_models=["secondary"])
    assert ok and ("secondary", "A") in r.calls
    assert r.coverage.reviewed_ratio == 1.0


@pytest.mark.asyncio
async def test_exhausted_chunk_marks_files_failed(monkeypatch):
    plans = [ChunkPlan("A", ("a.py",), ()), ChunkPlan("B", ("b.py",), ())]
    monkeypatch.setattr(mod, "split_chunk_plan", lambda plan, *a: [plan])
    monkeypatch.setattr(mod, "CHUNK_REVIEW_ATTEMPTS", 1)
    r = _Reviewer(plans, failures={"A"})
    r._failures_secondary = True

    async def always_fail_a(model, patches_diff=None, chunk_index=None, files=None):
        if patches_diff == "A":
            raise RuntimeError("boom")
        return ("raw", {"review": {"key_issues_to_review": []}}, 0)
    r._get_review_data = always_fail_a
    ok = await r._review_chunk_plans("primary", fallback_models=["secondary"])
    assert ok
    assert r.coverage.files["a.py"].status == "chunk_failed"
    assert r.review_failed_chunk_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_review_chunk_recovery.py -q`
Expected: FAIL (`_review_chunk_plans` / `split_chunk_plan` missing)

- [ ] **Step 3: Implement**

Extract the retry loop of `_prepare_chunked_prediction` into `async def _review_chunk_plans(self, model, fallback_models) -> bool` operating on `self.chunk_plans`. Stages, each over the still-pending plans:

1. `CHUNK_REVIEW_ATTEMPTS` attempts on `model` (existing loop, now keyed by plan object, not index).
2. If `pr_reviewer.chunk_split_on_failure`: for each pending plan with more than one file, `halves = split_chunk_plan(plan, self.git_provider, self.token_handler, model)`; replace the plan with its halves in the pending list and run one attempt on `model`.
3. If `pr_reviewer.chunk_fallback_model_on_failure` and `fallback_models`: one attempt on `fallback_models[0]` for whatever is still pending.
4. Anything still pending: `for f in plan.files: self.coverage.mark(f, "chunk_failed")`; count toward `review_failed_chunk_count`.

Merge successful outputs in diff order using the plan list order (halves keep their parent's position). `split_chunk_plan` filters `git_provider.get_diff_files()` to `plan.files[:mid]` and `plan.files[mid:]` and calls `get_pr_multi_diffs_with_files` with `max_calls=1` on each subset; if a subset still exceeds one chunk, take its first plan. `_prepare_chunked_prediction` becomes: build plans and coverage, then `return await self._review_chunk_plans(model, get_settings().config.get("fallback_models", []))`.

`configuration.toml` `[pr_reviewer]`:

```toml
# On a chunk that fails every attempt: split it in half by file and retry, then try the first fallback model,
# and only then mark its files as not reviewed.
chunk_split_on_failure = true
chunk_fallback_model_on_failure = true
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_review_chunk_recovery.py tests/unittest/test_review_consensus_partiality.py tests/unittest/test_pr_reviewer_core.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix pr_agent/tools/pr_reviewer.py tests/unittest/test_review_chunk_recovery.py
git add pr_agent/tools/pr_reviewer.py pr_agent/settings/configuration.toml tests/unittest/test_review_chunk_recovery.py
git commit -m "feat(review): split failed chunks and try a fallback model before giving up on their files

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Premise verification pass (R-8)

**Files:**
- Create: `pr_agent/algo/finding_verifier.py`
- Create: `pr_agent/settings/pr_finding_verifier_prompts.toml`
- Modify: `pr_agent/config_loader.py:20-44` (register prompt file)
- Modify: `pr_agent/settings/configuration.toml` `[pr_reviewer]` (`enable_finding_verification = false`, `verify_max_findings = 10`, `verification_model = ""`)
- Modify: `pr_agent/tools/pr_reviewer.py:1035-1075` (call verifier before state reconcile)
- Test: `tests/unittest/test_finding_verifier.py`

**Interfaces:**
- Produces: `HEDGE_RE`; `referenced_pr_files(issue: dict, pr_files: Iterable[str]) -> list[str]`; `build_verification_context(issue, own_file_text: str, other_files: dict[str, str], max_chars=40000) -> str`; `Verdict(status: Literal["confirmed","refuted","unverified"], evidence: str, reason: str)`; `parse_verdict(text: str) -> Verdict`; `async verify_findings(issues: list[dict], fetch_file: Callable[[str], Awaitable[str]], pr_files, call_model: Callable[[str, str], Awaitable[str]], max_findings: int) -> list[tuple[dict, Verdict]]`.
- Consumes: `git_provider.get_pr_file_content(path, branch)` (GitHub provider has it; base falls back to `get_repo_file_content`); `self.ai_handler.chat_completion(model, system, user, temperature=0.0, stage="verify")` from Task 2.

- [ ] **Step 1: Write the failing test**

```python
# tests/unittest/test_finding_verifier.py
import pytest

from pr_agent.algo.finding_verifier import (HEDGE_RE, Verdict, build_verification_context, parse_verdict,
                                            referenced_pr_files, verify_findings)

HEDGED = {"relevant_file": "lib/src/profile/profile_providers.dart", "start_line": 116, "end_line": 120,
          "issue_header": "Duplicate History",
          "issue_content": "Unless the first Game Over is suppressed elsewhere, Recent Runs will show two entries."}
CONFIDENT = {"relevant_file": "lib/src/game/piece_tray.dart", "start_line": 400, "end_line": 401,
             "issue_header": "Possible Issue",
             "issue_content": "If drop resolution in `board_view.dart` still depends on `grip`, drags will land offset."}


def test_hedge_regex_flags_both_phrasings():
    assert HEDGE_RE.search(HEDGED["issue_content"])
    assert HEDGE_RE.search(CONFIDENT["issue_content"])
    assert not HEDGE_RE.search("The list is indexed past its length on line 4.")


def test_referenced_pr_files_by_basename():
    files = ["lib/src/game/board_view.dart", "lib/src/game/piece_tray.dart", "lib/x.dart"]
    assert referenced_pr_files(CONFIDENT, files) == ["lib/src/game/board_view.dart"]


def test_context_includes_own_file_and_referenced_files_within_budget():
    ctx = build_verification_context(CONFIDENT, "OWN" * 10, {"lib/src/game/board_view.dart": "OTHER" * 10}, max_chars=200)
    assert "piece_tray.dart" in ctx and "board_view.dart" in ctx
    assert len(ctx) <= 200 + 100  # headers allowed beyond budget only


@pytest.mark.parametrize("text,status", [
    ('{"status": "refuted", "evidence": "runs[0] = entry;", "reason": "same seed replaces"}', "refuted"),
    ('```json\n{"status":"confirmed","evidence":"x","reason":"y"}\n```', "confirmed"),
    ("garbage", "unverified"),
])
def test_parse_verdict(text, status):
    assert parse_verdict(text).status == status


@pytest.mark.asyncio
async def test_verify_findings_drops_refuted_keeps_others():
    async def fetch(path):
        return f"content of {path}"

    async def call_model(system, user):
        if "Duplicate History" in user:
            return '{"status":"refuted","evidence":"runs[0] = entry;","reason":"replaces same seed"}'
        return '{"status":"unverified","evidence":"","reason":"not enough context"}'

    results = await verify_findings([HEDGED, CONFIDENT], fetch, ["lib/src/game/board_view.dart"], call_model, max_findings=10)
    statuses = {r[0]["issue_header"]: r[1].status for r in results}
    assert statuses == {"Duplicate History": "refuted", "Possible Issue": "unverified"}


@pytest.mark.asyncio
async def test_verify_respects_max_findings():
    async def fetch(path):
        return ""

    calls = []

    async def call_model(system, user):
        calls.append(1)
        return '{"status":"confirmed","evidence":"e","reason":"r"}'

    results = await verify_findings([HEDGED, CONFIDENT, dict(HEDGED)], fetch, [], call_model, max_findings=2)
    assert len(calls) == 2 and len(results) == 3
    assert results[2][1].status == "unverified"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_finding_verifier.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement the module**

```python
# pr_agent/algo/finding_verifier.py
"""Check each finding's premise against the files it depends on before publishing it.

The audit's four false positives were all guesses about an invariant in a file outside the diff.
Hedge words are logged as a signal; every finding within budget is verified regardless of wording.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Literal

HEDGE_RE = re.compile(
    r"\b(unless|assuming|depending on (?:how|whether)|if [^.]{0,80}\b(?:is|does|are|still|not)\b|may (?:still|not))\b",
    re.IGNORECASE,
)
_FILE_REF_RE = re.compile(r"`?([\w./-]+\.(?:dart|py|ts|tsx|js|kt|swift|go|java|rb|rs))`?")
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

Status = Literal["confirmed", "refuted", "unverified"]


@dataclass(frozen=True)
class Verdict:
    status: Status
    evidence: str = ""
    reason: str = ""


def referenced_pr_files(issue: dict, pr_files: Iterable[str]) -> list[str]:
    own = str(issue.get("relevant_file", ""))
    text = f"{issue.get('issue_header', '')} {issue.get('issue_content', '')}"
    names = {os.path.basename(m) for m in _FILE_REF_RE.findall(text)}
    return [p for p in pr_files if os.path.basename(p) in names and p != own]


def build_verification_context(issue: dict, own_file_text: str, other_files: dict[str, str],
                               max_chars: int = 40000) -> str:
    sections = [(str(issue.get("relevant_file", "")), own_file_text)] + list(other_files.items())
    budget_each = max(200, max_chars // max(1, len(sections)))
    parts = []
    for path, text in sections:
        body = text if len(text) <= budget_each else text[:budget_each] + "\n... [truncated]"
        parts.append(f"### {path}\n```\n{body}\n```")
    return "\n\n".join(parts)


def parse_verdict(text: str) -> Verdict:
    match = _JSON_RE.search(text or "")
    if not match:
        return Verdict("unverified", reason="unparsable verdict")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return Verdict("unverified", reason="unparsable verdict")
    status = str(data.get("status", "")).lower()
    if status not in ("confirmed", "refuted", "unverified"):
        status = "unverified"
    return Verdict(status, evidence=str(data.get("evidence", ""))[:500], reason=str(data.get("reason", ""))[:500])


async def verify_findings(issues: list[dict], fetch_file: Callable[[str], Awaitable[str]], pr_files: Iterable[str],
                          call_model: Callable[[str, str], Awaitable[str]], max_findings: int,
                          system_prompt: str = "", user_template: str = "") -> list[tuple[dict, Verdict]]:
    pr_files = list(pr_files)
    results: list[tuple[dict, Verdict]] = []
    for index, issue in enumerate(issues):
        if index >= max_findings:
            results.append((issue, Verdict("unverified", reason="verification budget exhausted")))
            continue
        own = await fetch_file(str(issue.get("relevant_file", "")))
        others = {p: await fetch_file(p) for p in referenced_pr_files(issue, pr_files)}
        context = build_verification_context(issue, own or "", others)
        user = (user_template or "{finding}\n\n{context}").format(
            finding=json.dumps(issue, ensure_ascii=False), context=context,
            hedged=bool(HEDGE_RE.search(str(issue.get("issue_content", "")))))
        try:
            raw = await call_model(system_prompt, user)
            verdict = parse_verdict(raw)
        except Exception as exc:  # a verifier failure must never lose a finding
            verdict = Verdict("unverified", reason=f"verifier error: {type(exc).__name__}")
        results.append((issue, verdict))
    return results
```

- [ ] **Step 4: Add the prompt file and register it**

```toml
# pr_agent/settings/pr_finding_verifier_prompts.toml
[pr_finding_verifier_prompt]
system="""You are a meticulous code reviewer verifying ONE finding produced by another reviewer.
You are given the finding and the full current content of the file it is about, plus any file it references.
Decide whether the finding's premise holds in this code.

Rules:
- "refuted" only when you can quote a line from the provided files that contradicts the finding's premise.
- "confirmed" only when you can quote a line that supports the causal claim (not merely the same code the finding points at).
- Otherwise "unverified". Do not guess. Missing context is "unverified", never "confirmed".
- Findings phrased with "unless", "if X is not", "assuming" are guesses about code outside the diff; check that code specifically.

Output exactly one JSON object and nothing else:
{"status": "confirmed" | "refuted" | "unverified", "evidence": "<one quoted line, or empty>", "reason": "<one sentence>"}
"""
user="""Finding (hedged wording detected: {{ hedged }}):
{{ finding }}

Files:
{{ context }}
"""
```

Add `"settings/pr_finding_verifier_prompts.toml",` to `settings_files` in `pr_agent/config_loader.py`. Render with Jinja (`Environment(undefined=StrictUndefined).from_string(...)`) in the reviewer, passing `finding`, `context`, `hedged`.

`configuration.toml` `[pr_reviewer]`:

```toml
# Verify each finding's premise against the full content of its file and any PR file it references,
# using a cheap model; refuted findings are dropped (logged with evidence), unverified ones are tagged.
enable_finding_verification = false
verify_max_findings = 10
verification_model = ""   # empty = config.model_weak, else config.model
```

- [ ] **Step 5: Wire into the reviewer**

In `_prepare_pr_review` (before `_prepare_review_finding_state(data)` at line 1071), when `enable_finding_verification`:

```python
        issues = list(data.get("review", {}).get("key_issues_to_review") or [])
        pr_files = [f.filename for f in self.git_provider.get_diff_files()]
        model = get_settings().pr_reviewer.get("verification_model") or get_settings().config.get("model_weak") \
            or get_settings().config.model

        async def fetch(path: str) -> str:
            try:
                return self.git_provider.get_pr_file_content(path, self.git_provider.pr.head.sha) or ""
            except Exception:
                return ""

        async def call_model(system: str, user: str) -> str:
            response, _ = await self.ai_handler.chat_completion(model, system, user, temperature=0.0, stage="verify")
            return response

        verified = await verify_findings(issues, fetch, pr_files, call_model,
                                         max_findings=int(get_settings().pr_reviewer.get("verify_max_findings", 10)),
                                         system_prompt=system_text, user_template=user_text)
        kept = []
        for issue, verdict in verified:
            if verdict.status == "refuted":
                get_logger().info("Dropping refuted finding", artifact={"issue": issue, "evidence": verdict.evidence})
                continue
            if verdict.status == "unverified":
                issue["issue_header"] = f"{issue.get('issue_header', '')} (unverified)".strip()
            kept.append(issue)
        data["review"]["key_issues_to_review"] = kept
        self.review_refuted_count = len(verified) - len(kept)
```

`_prepare_pr_review` is synchronous today; if so, run the verification in `run()` right after the prediction is ready and before `_prepare_pr_review`, storing the filtered `data` back into `self.prediction_data`. Use whichever provider method fetches a file at the head SHA (`get_pr_file_content` on GitHub; fall back to `get_repo_file_content(path)`).

- [ ] **Step 6: Run tests**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_finding_verifier.py tests/unittest/test_pr_reviewer_core.py tests/unittest/test_pr_reviewer_prompt_contract.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
uv run ruff check --fix pr_agent/algo/finding_verifier.py pr_agent/tools/pr_reviewer.py pr_agent/config_loader.py tests/unittest/test_finding_verifier.py
git add pr_agent/algo/finding_verifier.py pr_agent/settings/pr_finding_verifier_prompts.toml pr_agent/config_loader.py pr_agent/settings/configuration.toml pr_agent/tools/pr_reviewer.py tests/unittest/test_finding_verifier.py
git commit -m "feat(review): verify each finding's premise against its files with a cheap model; drop refuted ones

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Ship-scope priority ordering and ignore proposals (R-9)

**Files:**
- Create: `pr_agent/algo/ship_scope.py`
- Modify: `pr_agent/algo/pr_processing.py` (`get_pr_multi_diffs_with_files` accepts `priority_key`)
- Modify: `pr_agent/tools/pr_reviewer.py` (order files, mark `low_priority_summary`, footer proposal)
- Modify: `pr_agent/settings/configuration.toml` `[pr_reviewer]` (`low_priority_globs`, `low_priority_summarize_when_over_budget = true`)
- Test: `tests/unittest/test_ship_scope.py`

**Interfaces:**
- Produces: `is_low_priority(path: str, globs: Iterable[str]) -> bool`; `order_files_by_priority(files: list[FilePatchInfo], globs) -> list[FilePatchInfo]` (stable: high-priority first, original order within groups); `propose_ignore_globs(low_files: Iterable[str], tokens_by_file: Mapping[str, int]) -> list[tuple[str, int]]` (glob → tokens saved, sorted desc, one glob per top-level dir); `render_ignore_proposal(proposals) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unittest/test_ship_scope.py
from pr_agent.algo.ship_scope import is_low_priority, order_files_by_priority, propose_ignore_globs, render_ignore_proposal

GLOBS = ["docs/**", "design/**", "mockups/**", "**/fixtures/**", "**/*.md"]


class _F:
    def __init__(self, name):
        self.filename = name


def test_low_priority_matches_defaults():
    assert is_low_priority("design/_v_fever.html", GLOBS)
    assert is_low_priority("README.md", GLOBS)
    assert is_low_priority("test/fixtures/x.json", GLOBS)
    assert not is_low_priority("lib/src/iap/iap_providers.dart", GLOBS)


def test_order_is_stable_high_first():
    files = [_F("design/a.html"), _F("lib/a.dart"), _F("docs/b.md"), _F("lib/b.dart")]
    assert [f.filename for f in order_files_by_priority(files, GLOBS)] == ["lib/a.dart", "lib/b.dart", "design/a.html", "docs/b.md"]


def test_propose_ignore_globs_groups_by_top_dir_and_sorts_by_tokens():
    low = ["design/a.html", "design/b.html", "docs/c.md"]
    tokens = {"design/a.html": 30000, "design/b.html": 25000, "docs/c.md": 2000}
    assert propose_ignore_globs(low, tokens) == [("design/**", 55000), ("docs/**", 2000)]


def test_render_proposal_is_toml_snippet():
    text = render_ignore_proposal([("design/**", 55000)])
    assert "[ignore]" in text and 'glob = ["design/**"]' in text and "55,000" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_ship_scope.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# pr_agent/algo/ship_scope.py
"""Files that probably do not ship (docs, mockups, fixtures) are reviewed last and summarized when budget is tight.

Never silently excluded: build scripts and dynamically loaded assets can affect production, so the
default only reorders. Ignoring is proposed to a human as a config snippet with the tokens it would save.
"""
from __future__ import annotations

import fnmatch
from collections import defaultdict
from typing import Iterable, Mapping

DEFAULT_LOW_PRIORITY_GLOBS = ("docs/**", "design/**", "mockups/**", "**/fixtures/**", "**/*.md")


def _match(path: str, glob: str) -> bool:
    if glob.endswith("/**"):
        prefix = glob[:-3]
        return path.startswith(prefix + "/") or path == prefix
    if glob.startswith("**/"):
        tail = glob[3:]
        return fnmatch.fnmatch(path, tail) or any(fnmatch.fnmatch(path[i + 1:], tail) for i, c in enumerate(path) if c == "/") \
            or fnmatch.fnmatch(path, glob)
    return fnmatch.fnmatch(path, glob)


def is_low_priority(path: str, globs: Iterable[str]) -> bool:
    return any(_match(path, g) for g in globs)


def order_files_by_priority(files: list, globs: Iterable[str]) -> list:
    globs = list(globs)
    high = [f for f in files if not is_low_priority(f.filename, globs)]
    low = [f for f in files if is_low_priority(f.filename, globs)]
    return high + low


def propose_ignore_globs(low_files: Iterable[str], tokens_by_file: Mapping[str, int]) -> list[tuple[str, int]]:
    by_dir: dict[str, int] = defaultdict(int)
    for path in low_files:
        top = path.split("/", 1)[0] if "/" in path else path
        by_dir[f"{top}/**" if "/" in path else path] += int(tokens_by_file.get(path, 0))
    return sorted(by_dir.items(), key=lambda kv: kv[1], reverse=True)


def render_ignore_proposal(proposals: list[tuple[str, int]]) -> str:
    if not proposals:
        return ""
    globs = ", ".join(f'"{g}"' for g, _ in proposals)
    lines = ["<details><summary>Suggested <code>.pr_agent.toml</code> ignore rules</summary>", "",
             "These files look like documentation or mockups. Reviewing them cost tokens without affecting shipped code:", ""]
    lines += [f"- `{g}` · ~{t:,} tokens" for g, t in proposals]
    lines += ["", "```toml", "[ignore]", f"glob = [{globs}]", "```", "</details>"]
    return "\n".join(lines)
```

Wire-up: in `_prepare_chunked_prediction` and the single-call path, call `order_files_by_priority(diff_files, globs)` before chunking (pass an ordered file list into `get_pr_multi_diffs_with_files` via a new optional `files=None` parameter that bypasses the internal `get_diff_files()` call when given). When `low_priority_summarize_when_over_budget` and a low-priority file lands in `remaining_files_list`, set its coverage status to `low_priority_summary` and add a one-line `"- design/_v_fever.html (mockup, not reviewed)"` list under the footer. Compute `tokens_by_file` with `self.token_handler.count_tokens(file.patch)`; append `render_ignore_proposal(propose_ignore_globs(low_paths, tokens_by_file))` to the footer when any low-priority file consumed tokens.

`configuration.toml` `[pr_reviewer]`:

```toml
# Files matching these globs are reviewed last and, when the token budget is tight, summarized in one line
# instead of reviewed. Nothing is dropped; an [ignore] snippet is proposed in the comment for a human to accept.
low_priority_globs = ["docs/**", "design/**", "mockups/**", "**/fixtures/**", "**/*.md"]
low_priority_summarize_when_over_budget = true
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_ship_scope.py tests/unittest/test_pr_processing_chunks.py tests/unittest/test_pr_reviewer_core.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix pr_agent/algo/ship_scope.py pr_agent/algo/pr_processing.py pr_agent/tools/pr_reviewer.py tests/unittest/test_ship_scope.py
git add pr_agent/algo/ship_scope.py pr_agent/algo/pr_processing.py pr_agent/tools/pr_reviewer.py pr_agent/settings/configuration.toml tests/unittest/test_ship_scope.py
git commit -m "feat(review): review likely-unshipped files last, summarize them under budget, propose ignore globs

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Baseline and gate

**Files:**
- Create: `tests/eval/BASELINE.md`
- Modify: `tests/eval/README.md`

- [ ] **Step 1: Full unit suite**

Run: `PYTHONPATH=. uv run pytest tests/unittest -q 2>&1 | tail -5`
Expected: all pass; note the count.

- [ ] **Step 2: Fetch the block_rush diff and run the labeled eval twice**

Requires a configured model key in the environment (same as the earlier smoke tests). Run once with today's defaults and once with the new flags on:

```bash
tests/eval/fetch_pr_diff.sh samer2373/block_rush 1 /tmp/block_rush_pr1.diff
PYTHONPATH=. uv run python tests/eval/run_eval.py --labels tests/eval/labels/block_rush_pr1.json \
  --diff-file /tmp/block_rush_pr1.diff --out /tmp/baseline_default.json
PYTHONPATH=. uv run python tests/eval/run_eval.py --labels tests/eval/labels/block_rush_pr1.json \
  --diff-file /tmp/block_rush_pr1.diff \
  --set pr_reviewer.enable_finding_verification=true --set pr_reviewer.enable_large_pr_chunking=true \
  --set config.run_ledger_path=/tmp/ledger.jsonl --out /tmp/baseline_p0.json
```

- [ ] **Step 3: Record the baseline**

Write `tests/eval/BASELINE.md` with a table of `precision, recall, severity_weighted_recall, control_false_flags, unknown, total tokens (from ledger), tokens on design/** (sum ledger rows whose files all match design/**)` for both runs, the model used, the date, and the commit SHA. State plainly which numbers moved and which did not. Every later plan (P1 `/setup`, P2 retrieval) must add a row here before merging.

- [ ] **Step 4: Commit**

```bash
git add tests/eval/BASELINE.md tests/eval/README.md
git commit -m "docs(eval): record P0 baseline on block_rush#1 labeled corpus

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage:** R-1 → Task 1; R-2 → Task 2; R-3 → Task 3; R-4 → Task 6; R-5 → Task 5; R-6 → Task 4; R-7 → Task 7; R-8 → Task 8; R-9 → Task 9. R-10 onward are out of P0 scope by the spec.

**Type consistency:** `ChunkPlan(diff, files, clipped)` defined in Task 3 and consumed in Tasks 7 and 9; `CoverageLedger.mark/add/files/reviewed_ratio/render_footer` used identically in Tasks 3, 5, 7, 9; `record_ai_call(..., stage=, chunk_index=, sample_index=, files=, latency_ms=)` from Task 2 used in Task 8 via `chat_completion(..., stage="verify")`; `reconcile_review_findings(..., fully_reviewed_files=)` from Task 5 used in Task 6; `same_finding_across_runs` from Task 4 used in Task 5's reconcile loop via `matched_previous_ids`.

**Known judgment calls the executor should not re-open:** clipped files earn 0.5 credit (Task 3); resolution needs the file fully reviewed, not lines changed (Task 5, v1 evidence rule); verification runs on every finding within budget, hedge words only logged (Task 8); low-priority files are reordered and summarized, never dropped (Task 9).
