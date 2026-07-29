# Core Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the team lead's GitHub PR-to-inline-comment control plane, beginning with frozen mock contracts that unblock the Agent and static-analysis owners.

**Architecture:** Add an isolated `assessment.ai_reviewer` package and integration fixtures without modifying upstream PR-Agent internals. GitHub reads flow through a narrow upstream adapter; all writes flow through the publisher; deterministic guards constrain line scope, idempotency, budget, and artifacts before the workflow is enabled.

**Tech Stack:** Python 3.12, pytest 9, PyGithub 1.59, unidiff 0.7.5, GitHub Actions, JSON fixtures, Ruff/flake8.

---

## File Map

- `assessment/__init__.py`: assessment package marker.
- `assessment/ai_reviewer/__init__.py`: public contract exports.
- `assessment/ai_reviewer/contracts.py`: frozen shared request, finding, and result objects.
- `assessment/ai_reviewer/budget.py`: monotonic analysis deadline.
- `assessment/ai_reviewer/diff_scope.py`: changed-head-line extraction and finding guard.
- `assessment/ai_reviewer/github_runtime.py`: strict pull request event parsing.
- `assessment/ai_reviewer/upstream_adapter.py`: stable wrapper over PR-Agent's GitHub provider.
- `assessment/ai_reviewer/publisher.py`: the only inline-comment publication path.
- `assessment/ai_reviewer/run_artifact.py`: atomic, redacted JSON artifact writer.
- `assessment/ai_reviewer/cli.py`: dependency-injected orchestration and explicit probe/full modes.
- `assessment/tests/integration/fixtures.py`: downstream fixture loader.
- `assessment/tests/integration/fixtures/*.json|*.diff`: shared mock inputs and outputs.
- `assessment/tests/integration/test_*.py`: offline tests for every core boundary.
- `.github/workflows/ai-review.yml`: controlled PR workflow and artifact upload.

## Task 1: Freeze Contracts and Publish Downstream Mock Data

**Files:**
- Create: `assessment/__init__.py`
- Create: `assessment/ai_reviewer/__init__.py`
- Create: `assessment/ai_reviewer/contracts.py`
- Create: `assessment/tests/__init__.py`
- Create: `assessment/tests/integration/__init__.py`
- Create: `assessment/tests/integration/fixtures.py`
- Create: `assessment/tests/integration/fixtures/pull_request_event.json`
- Create: `assessment/tests/integration/fixtures/sample.diff`
- Create: `assessment/tests/integration/fixtures/review_request.json`
- Create: `assessment/tests/integration/fixtures/static_findings.json`
- Create: `assessment/tests/integration/fixtures/agent_findings.json`
- Create: `assessment/tests/integration/fixtures/review_result.json`
- Create: `assessment/tests/integration/fixtures/README.md`
- Test: `assessment/tests/integration/test_contracts.py`

- [ ] **Step 1: Create the Python 3.12 environment**

Run:

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt -r requirements-dev.txt
uv pip install --python .venv\Scripts\python.exe ruff==0.7.1
```

Expected: `.venv\Scripts\python.exe --version` reports Python 3.12.x and pytest imports successfully.

- [ ] **Step 2: Write failing contract and fixture tests**

```python
# assessment/tests/integration/test_contracts.py
from dataclasses import FrozenInstanceError

import pytest

from assessment.ai_reviewer.contracts import Finding, ReviewRequest, ReviewResult
from assessment.tests.integration.fixtures import load_json, load_request, load_result


def test_shared_request_fixture_round_trips() -> None:
    request = load_request()
    assert request.repository == "JiXia830/pr-agent"
    assert request.changed_lines["src/calculator.py"] == frozenset({10, 11, 12})
    assert request.to_dict() == load_json("review_request.json")


def test_downstream_findings_cover_every_category() -> None:
    raw = load_json("static_findings.json") + load_json("agent_findings.json")
    findings = [Finding.from_dict(item) for item in raw]
    assert {finding.category for finding in findings} == {
        "static", "business_logic", "logic", "memory", "security", "architecture"
    }
    assert {finding.source for finding in findings} == {"agent", "semgrep", "syntax"}


def test_contracts_reject_invalid_enum_and_confidence() -> None:
    item = load_json("agent_findings.json")[0]
    with pytest.raises(ValueError, match="category"):
        Finding.from_dict({**item, "category": "style"})
    with pytest.raises(ValueError, match="confidence"):
        Finding.from_dict({**item, "confidence": 1.1})


def test_contract_is_frozen_and_defensively_copied() -> None:
    request = load_request()
    with pytest.raises(FrozenInstanceError):
        request.repository = "other/repo"


def test_result_fixture_round_trips() -> None:
    result = load_result()
    assert isinstance(result, ReviewResult)
    assert result.status == "success"
    assert result.to_dict() == load_json("review_result.json")
```

- [ ] **Step 3: Run the tests and verify the import failure**

Run:

```powershell
$env:PYTHONPATH = "."
.venv\Scripts\python.exe -m pytest assessment/tests/integration/test_contracts.py -q
```

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'assessment.ai_reviewer.contracts'`.

- [ ] **Step 4: Implement the frozen contracts**

```python
# assessment/ai_reviewer/contracts.py
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

CATEGORIES = frozenset({"static", "business_logic", "logic", "memory", "security", "architecture"})
SEVERITIES = frozenset({"low", "medium", "high", "critical"})
SOURCES = frozenset({"agent", "semgrep", "syntax"})
STATUSES = frozenset({"success", "failed", "timeout"})


def _required_text(name: str, value: str) -> str:
    value = str(value).strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    title: str
    body: str
    diff: str
    changed_lines: Mapping[str, frozenset[int]]
    changed_files: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository", _required_text("repository", self.repository))
        if self.pr_number <= 0:
            raise ValueError("pr_number must be positive")
        object.__setattr__(self, "base_sha", _required_text("base_sha", self.base_sha))
        object.__setattr__(self, "head_sha", _required_text("head_sha", self.head_sha))
        copied = {str(path): frozenset(int(line) for line in lines) for path, lines in self.changed_lines.items()}
        object.__setattr__(self, "changed_lines", copied)
        object.__setattr__(self, "changed_files", tuple(str(path) for path in self.changed_files))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReviewRequest":
        return cls(**{**value, "changed_lines": value["changed_lines"], "changed_files": value["changed_files"]})

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["changed_lines"] = {path: sorted(lines) for path, lines in self.changed_lines.items()}
        value["changed_files"] = list(self.changed_files)
        return value


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    category: str
    severity: str
    confidence: float
    title: str
    evidence: tuple[str, ...]
    impact: str
    suggestion: str
    source: str

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"invalid category: {self.category}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"invalid severity: {self.severity}")
        if self.source not in SOURCES:
            raise ValueError(f"invalid source: {self.source}")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be finite and between 0 and 1")
        object.__setattr__(self, "path", _required_text("path", self.path))
        object.__setattr__(self, "title", _required_text("title", self.title))
        object.__setattr__(self, "evidence", tuple(str(item) for item in self.evidence))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Finding":
        return cls(**{**value, "evidence": tuple(value["evidence"])})

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence"] = list(self.evidence)
        return value


@dataclass(frozen=True, slots=True)
class ReviewResult:
    run_id: str
    model: str
    status: str
    started_at: str
    finished_at: str
    duration_seconds: float
    findings: tuple[Finding, ...]
    errors: tuple[str, ...]
    trace_summary: tuple[dict[str, Any], ...]

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"invalid status: {self.status}")
        object.__setattr__(self, "run_id", _required_text("run_id", self.run_id))
        object.__setattr__(self, "model", _required_text("model", self.model))
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "errors", tuple(str(error) for error in self.errors))
        object.__setattr__(self, "trace_summary", tuple(dict(item) for item in self.trace_summary))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReviewResult":
        return cls(
            **{
                **value,
                "findings": tuple(Finding.from_dict(item) for item in value["findings"]),
                "errors": tuple(value["errors"]),
                "trace_summary": tuple(value["trace_summary"]),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "model": self.model,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "findings": [finding.to_dict() for finding in self.findings],
            "errors": list(self.errors),
            "trace_summary": [dict(item) for item in self.trace_summary],
        }
```

Export `Finding`, `ReviewRequest`, and `ReviewResult` from `assessment/ai_reviewer/__init__.py`. Keep the two package marker files empty.

- [ ] **Step 5: Add the fixture loader**

```python
# assessment/tests/integration/fixtures.py
import json
from pathlib import Path
from typing import Any

from assessment.ai_reviewer.contracts import ReviewRequest, ReviewResult

FIXTURE_ROOT = Path(__file__).with_name("fixtures")


def load_json(name: str) -> Any:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def load_request() -> ReviewRequest:
    return ReviewRequest.from_dict(load_json("review_request.json"))


def load_result() -> ReviewResult:
    return ReviewResult.from_dict(load_json("review_result.json"))
```

- [ ] **Step 6: Add exact downstream fixture shapes**

Use repository `JiXia830/pr-agent`, PR `17`, base SHA of forty `a` characters, head SHA of forty `b` characters, and changed file `src/calculator.py` on new lines 10-12 in every fixture. Create the files with these exact shapes:

`pull_request_event.json`:

```json
{
  "action": "synchronize",
  "repository": {"full_name": "JiXia830/pr-agent"},
  "pull_request": {
    "number": 17,
    "title": "Handle zero divisors",
    "body": "Synthetic contract fixture",
    "base": {"sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    "head": {
      "sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "repo": {"full_name": "JiXia830/pr-agent"}
    }
  }
}
```

`sample.diff`:

```diff
diff --git a/src/calculator.py b/src/calculator.py
index 1111111..2222222 100644
--- a/src/calculator.py
+++ b/src/calculator.py
@@ -9,1 +9,4 @@
 def divide(total, count):
+    if count == 0:
+        raise ValueError("count must not be zero")
+    return total / count
```

`review_request.json`:

```json
{
  "repository": "JiXia830/pr-agent",
  "pr_number": 17,
  "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "title": "Handle zero divisors",
  "body": "Synthetic contract fixture",
  "diff": "diff --git a/src/calculator.py b/src/calculator.py\nindex 1111111..2222222 100644\n--- a/src/calculator.py\n+++ b/src/calculator.py\n@@ -9,1 +9,4 @@\n def divide(total, count):\n+    if count == 0:\n+        raise ValueError(\"count must not be zero\")\n+    return total / count\n",
  "changed_lines": {"src/calculator.py": [10, 11, 12]},
  "changed_files": ["src/calculator.py"]
}
```

`static_findings.json`:

```json
[
  {
    "path": "src/calculator.py",
    "line": 10,
    "category": "static",
    "severity": "low",
    "confidence": 0.93,
    "title": "Redundant branch in synthetic fixture",
    "evidence": ["The added branch duplicates a validated precondition."],
    "impact": "The branch increases maintenance cost.",
    "suggestion": "Keep the validation in one location.",
    "source": "syntax"
  },
  {
    "path": "src/calculator.py",
    "line": 11,
    "category": "security",
    "severity": "high",
    "confidence": 0.88,
    "title": "Sensitive value may reach an exception",
    "evidence": ["The synthetic exception path receives an unredacted input."],
    "impact": "A caller could expose sensitive input through logs.",
    "suggestion": "Raise a fixed message and redact caller context.",
    "source": "semgrep"
  }
]
```

`agent_findings.json`:

```json
[
  {
    "path": "src/calculator.py", "line": 10, "category": "business_logic", "severity": "medium",
    "confidence": 0.84, "title": "Zero has domain-specific meaning",
    "evidence": ["The caller contract distinguishes missing counts from zero counts."],
    "impact": "Valid zero-count reports could be rejected.", "suggestion": "Confirm the domain rule before rejecting zero.",
    "source": "agent"
  },
  {
    "path": "src/calculator.py", "line": 12, "category": "logic", "severity": "high",
    "confidence": 0.91, "title": "Integer callers now receive a float",
    "evidence": ["The added division always uses true division."],
    "impact": "Downstream equality checks can change behavior.", "suggestion": "Preserve the documented numeric return type.",
    "source": "agent"
  },
  {
    "path": "src/calculator.py", "line": 12, "category": "memory", "severity": "low",
    "confidence": 0.72, "title": "Unbounded numeric result retention",
    "evidence": ["The synthetic caller retains every calculated result."],
    "impact": "Long-running batches can retain unnecessary objects.", "suggestion": "Stream or bound retained results in the caller.",
    "source": "agent"
  },
  {
    "path": "src/calculator.py", "line": 10, "category": "architecture", "severity": "medium",
    "confidence": 0.79, "title": "Domain validation is coupled to arithmetic",
    "evidence": ["The arithmetic helper now decides a reporting-domain policy."],
    "impact": "Other callers cannot reuse the calculation with a different policy.",
    "suggestion": "Validate domain rules at the service boundary.", "source": "agent"
  }
]
```

`review_result.json`:

```json
{
  "run_id": "00000000-0000-4000-8000-000000000017",
  "model": "glm-5.2",
  "status": "success",
  "started_at": "2026-07-29T10:00:00Z",
  "finished_at": "2026-07-29T10:00:02.500000Z",
  "duration_seconds": 2.5,
  "findings": [
    {
      "path": "src/calculator.py", "line": 11, "category": "security", "severity": "high",
      "confidence": 0.88, "title": "Sensitive value may reach an exception",
      "evidence": ["The synthetic exception path receives an unredacted input."],
      "impact": "A caller could expose sensitive input through logs.",
      "suggestion": "Raise a fixed message and redact caller context.", "source": "semgrep"
    },
    {
      "path": "src/calculator.py", "line": 12, "category": "logic", "severity": "high",
      "confidence": 0.91, "title": "Integer callers now receive a float",
      "evidence": ["The added division always uses true division."],
      "impact": "Downstream equality checks can change behavior.",
      "suggestion": "Preserve the documented numeric return type.", "source": "agent"
    }
  ],
  "errors": [],
  "trace_summary": [
    {"stage": "analyze", "status": "completed"},
    {"stage": "context_request", "status": "completed", "path": "src/calculator.py"},
    {"stage": "tool_result", "status": "completed", "bytes": 128},
    {"stage": "review", "status": "completed", "finding_count": 2},
    {"stage": "verify", "status": "completed", "publishable_count": 2}
  ]
}
```

`static_findings.json` covers `static`/`syntax` and `security`/`semgrep`; `agent_findings.json` covers the remaining four categories with `source: agent`. `review_result.json` demonstrates mixed-source aggregation and every required trace stage without source contents or secrets.

`fixtures/README.md` documents:

```markdown
# Core Integration Fixtures

These files are the frozen mock boundary for the Agent and static-analysis branches.

- Read `review_request.json` as the input contract.
- Return arrays shaped like `agent_findings.json` or `static_findings.json`.
- Do not add fields without team-lead approval.
- `changed_lines` uses JSON arrays but becomes `frozenset[int]` in Python.
- Findings must target a listed changed line and use the frozen category, severity, and source enums.
- Static interface: `scan(request: ReviewRequest, budget: Budget) -> list[Finding]`.
- Agent interface: `review(request: ReviewRequest, static_findings: list[Finding], budget: Budget) -> tuple[list[Finding], list[dict]]`.
- Fixtures contain synthetic code only and no API keys, tokens, local paths, or benchmark ground truth.
```

- [ ] **Step 7: Run the contract tests**

Run:

```powershell
$env:PYTHONPATH = "."
.venv\Scripts\python.exe -m pytest assessment/tests/integration/test_contracts.py -q
```

Expected: `5 passed`.

- [ ] **Step 8: Commit and push the downstream contract checkpoint**

```powershell
git add assessment
git commit -m "feat: freeze review contracts and fixtures"
git push -u origin feat/core-integration
```

Expected: the remote branch contains the contract commit. Send its exact SHA to members A and B so they can branch or rebase immediately.

## Task 2: Enforce the 540-Second Analysis Budget

**Files:**
- Create: `assessment/ai_reviewer/budget.py`
- Test: `assessment/tests/integration/test_budget.py`

- [ ] **Step 1: Write the failing monotonic-clock tests**

```python
from assessment.ai_reviewer.budget import Budget, BudgetExpired


class FakeClock:
    value = 100.0

    def __call__(self) -> float:
        return self.value


def test_budget_refuses_new_work_after_deadline() -> None:
    clock = FakeClock()
    budget = Budget(limit_seconds=540.0, clock=clock)
    clock.value = 640.0
    assert budget.remaining_seconds() == 0.0
    clock.value = 641.0
    try:
        budget.require_start("context request")
    except BudgetExpired as error:
        assert "context request" in str(error)
    else:
        raise AssertionError("deadline must reject new work")


def test_wall_clock_changes_cannot_extend_budget() -> None:
    clock = FakeClock()
    budget = Budget(limit_seconds=10.0, clock=clock)
    clock.value = 105.0
    assert budget.remaining_seconds() == 5.0
```

- [ ] **Step 2: Verify the tests fail**

Run: `.venv\Scripts\python.exe -m pytest assessment/tests/integration/test_budget.py -q`

Expected: FAIL with missing `assessment.ai_reviewer.budget`.

- [ ] **Step 3: Implement `Budget` and `BudgetExpired`**

```python
# assessment/ai_reviewer/budget.py
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic


class BudgetExpired(RuntimeError):
    """Raised when analysis must not start more work."""


@dataclass(frozen=True, slots=True)
class Budget:
    limit_seconds: float = 540.0
    clock: Callable[[], float] = monotonic
    _deadline: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.limit_seconds <= 0:
            raise ValueError("limit_seconds must be positive")
        object.__setattr__(self, "_deadline", self.clock() + self.limit_seconds)

    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - self.clock())

    def require_start(self, operation: str) -> None:
        if self.remaining_seconds() <= 0:
            raise BudgetExpired(f"budget exhausted before {operation}")
```

- [ ] **Step 4: Run and commit**

Run: `.venv\Scripts\python.exe -m pytest assessment/tests/integration/test_budget.py -q`

Expected: `2 passed`.

```powershell
git add assessment/ai_reviewer/budget.py assessment/tests/integration/test_budget.py
git commit -m "feat: enforce analysis deadline"
```

## Task 3: Parse Diff Scope and Guard Publishable Findings

**Files:**
- Create: `assessment/ai_reviewer/diff_scope.py`
- Test: `assessment/tests/integration/test_diff_scope.py`
- Test: `assessment/tests/integration/test_publish_guard.py`

- [ ] **Step 1: Write failing diff tests using `fixtures/sample.diff`**

Test an added line, modified hunk, deleted-only file, rename, path traversal rejection, and old/new line separation. Assert that only target-side added lines enter `changed_lines` and all normalized files enter `changed_files`.

- [ ] **Step 2: Write failing guard tests**

Create findings on line 10 and line 99. Assert `filter_publishable()` keeps line 10, rejects line 99, rejects empty evidence/impact/suggestion, sorts critical/high before lower severity, and caps output at eight.

- [ ] **Step 3: Verify both test files fail**

Run:

```powershell
.venv\Scripts\python.exe -m pytest assessment/tests/integration/test_diff_scope.py assessment/tests/integration/test_publish_guard.py -q
```

Expected: FAIL with missing `assessment.ai_reviewer.diff_scope`.

- [ ] **Step 4: Implement structured parsing with `unidiff.PatchSet`**

```python
from pathlib import PurePosixPath
from typing import Iterable

from unidiff import PatchSet

from .contracts import Finding

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _safe_path(path: str) -> str:
    value = path.replace("\\", "/")
    if value.startswith(("a/", "b/")):
        value = value[2:]
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts or value in {"", "/dev/null"}:
        raise ValueError(f"unsafe diff path: {path}")
    return candidate.as_posix()


def parse_changed_scope(diff: str) -> tuple[tuple[str, ...], dict[str, frozenset[int]]]:
    files: list[str] = []
    lines: dict[str, set[int]] = {}
    for patched_file in PatchSet(diff.splitlines(keepends=True)):
        raw_path = patched_file.target_file if patched_file.target_file != "/dev/null" else patched_file.source_file
        path = _safe_path(raw_path)
        files.append(path)
        target_lines = lines.setdefault(path, set())
        for hunk in patched_file:
            target_lines.update(line.target_line_no for line in hunk if line.is_added and line.target_line_no is not None)
    return tuple(dict.fromkeys(files)), {path: frozenset(value) for path, value in lines.items()}


def filter_publishable(findings: Iterable[Finding], changed_lines: dict[str, frozenset[int]], limit: int = 8) -> tuple[Finding, ...]:
    valid = [
        finding
        for finding in findings
        if finding.path in changed_lines
        and finding.line in changed_lines[finding.path]
        and bool(finding.evidence)
        and bool(finding.impact.strip())
        and bool(finding.suggestion.strip())
    ]
    valid.sort(key=lambda item: (SEVERITY_ORDER[item.severity], -item.confidence, item.path, item.line, item.title))
    return tuple(valid[:limit])
```

- [ ] **Step 5: Run and commit**

Expected: all diff and guard tests pass.

```powershell
git add assessment/ai_reviewer/diff_scope.py assessment/tests/integration/test_diff_scope.py assessment/tests/integration/test_publish_guard.py
git commit -m "feat: constrain review findings to changed lines"
```

## Task 4: Parse GitHub Events and Wrap Upstream Reads

**Files:**
- Create: `assessment/ai_reviewer/github_runtime.py`
- Create: `assessment/ai_reviewer/upstream_adapter.py`
- Test: `assessment/tests/integration/test_github_runtime.py`
- Test: `assessment/tests/integration/test_upstream_adapter.py`

- [ ] **Step 1: Write strict event tests**

Load `pull_request_event.json`; assert action, repository, PR number, base/head SHA, title, body, and `same_repository=True`. Add cases for unsupported action, missing SHA, malformed JSON, and an external fork head.

- [ ] **Step 2: Write adapter tests with fake PR-Agent provider objects**

The fake provider exposes `pr`, `get_files()`, and `last_commit_id`. Test added, removed, renamed, and patch-less files; assert the adapter builds parseable unified diff without logging token or file contents.

- [ ] **Step 3: Verify failure**

Run: `.venv\Scripts\python.exe -m pytest assessment/tests/integration/test_github_runtime.py assessment/tests/integration/test_upstream_adapter.py -q`

Expected: FAIL because both modules are absent.

- [ ] **Step 4: Implement `PullRequestEvent` and `load_event()`**

```python
# assessment/ai_reviewer/github_runtime.py
import json
import re
from dataclasses import dataclass
from pathlib import Path

ALLOWED_ACTIONS = frozenset({"opened", "reopened", "synchronize"})
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class PullRequestEvent:
    action: str
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    title: str
    body: str
    same_repository: bool


def _sha(name: str, value: object) -> str:
    text = str(value)
    if not SHA_PATTERN.fullmatch(text):
        raise ValueError(f"{name} must be a 40-character lowercase hex SHA")
    return text


def load_event(path: str | Path) -> PullRequestEvent:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    action = str(payload.get("action", ""))
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"unsupported pull request action: {action}")
    repository = str(payload["repository"]["full_name"])
    pull_request = payload["pull_request"]
    pr_number = int(pull_request["number"])
    if pr_number <= 0:
        raise ValueError("pull request number must be positive")
    head_repository = str(pull_request["head"]["repo"]["full_name"])
    return PullRequestEvent(
        action=action,
        repository=repository,
        pr_number=pr_number,
        base_sha=_sha("base_sha", pull_request["base"]["sha"]),
        head_sha=_sha("head_sha", pull_request["head"]["sha"]),
        title=str(pull_request.get("title") or ""),
        body=str(pull_request.get("body") or ""),
        same_repository=head_repository == repository,
    )
```

- [ ] **Step 5: Implement `UpstreamAdapter`**

```python
# assessment/ai_reviewer/upstream_adapter.py
from collections.abc import Iterable
from typing import Any

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.github_provider import GithubProvider

from .github_runtime import PullRequestEvent


class UpstreamAdapter:
    def __init__(self, provider: Any) -> None:
        self._provider = provider

    @classmethod
    def from_token(cls, event: PullRequestEvent, token: str) -> "UpstreamAdapter":
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        settings = get_settings()
        settings.set("GITHUB.USER_TOKEN", token)
        settings.set("GITHUB.DEPLOYMENT_TYPE", "user")
        url = f"https://github.com/{event.repository}/pull/{event.pr_number}"
        return cls(GithubProvider(url))

    def unified_diff(self) -> str:
        chunks: list[str] = []
        for changed_file in self._provider.get_files():
            path = str(changed_file.filename)
            previous = str(getattr(changed_file, "previous_filename", path))
            status = str(changed_file.status)
            old_path = "/dev/null" if status == "added" else f"a/{previous}"
            new_path = "/dev/null" if status == "removed" else f"b/{path}"
            chunks.extend(
                [
                    f"diff --git a/{previous} b/{path}\n",
                    f"--- {old_path}\n",
                    f"+++ {new_path}\n",
                ]
            )
            patch = getattr(changed_file, "patch", None)
            if patch:
                chunks.append(str(patch).rstrip("\n") + "\n")
        return "".join(chunks)

    def current_head_sha(self) -> str:
        return str(self._provider.pr.head.sha)

    def existing_review_bodies(self) -> tuple[str, ...]:
        actor = self._provider.github_client.get_user().login
        return tuple(
            str(comment.body or "")
            for comment in self._provider.pr.get_review_comments()
            if comment.user and comment.user.login == actor
        )

    def create_review(self, head_sha: str, comments: Iterable[dict[str, object]]) -> None:
        commit = self._provider._get_repo().get_commit(head_sha)
        self._provider.pr.create_review(commit=commit, comments=list(comments))
```

`create_review()` remains on the adapter only to isolate the upstream object. `publisher.py` is its sole caller; scanners and Agent code receive only `ReviewRequest`.

- [ ] **Step 6: Run and commit**

Expected: event and adapter tests pass with no network access.

```powershell
git add assessment/ai_reviewer/github_runtime.py assessment/ai_reviewer/upstream_adapter.py assessment/tests/integration/test_github_runtime.py assessment/tests/integration/test_upstream_adapter.py
git commit -m "feat: load pull request context through upstream adapter"
```

## Task 5: Publish Idempotent Inline Reviews

**Files:**
- Create: `assessment/ai_reviewer/publisher.py`
- Create: `assessment/tests/integration/test_idempotency.py`
- Extend: `assessment/tests/integration/test_publish_guard.py`

- [ ] **Step 1: Write failing fingerprint and stale-head tests**

Assert `run_key(repository, pr, head_sha)` is stable, `finding_fingerprint()` changes when head/path/line/category/source/title changes, a marker already returned by the fake adapter is skipped, a changed head publishes nothing, and a maximum of eight comments is sent in one review.

- [ ] **Step 2: Write an unknown-response reconciliation test**

Make fake `create_review()` add marker bodies and then raise `TimeoutError`. Assert publisher re-lists once, sees all markers, reports them as published, and never calls `create_review()` a second time. If markers are absent, assert the exception propagates without replay.

- [ ] **Step 3: Verify failure**

Run: `.venv\Scripts\python.exe -m pytest assessment/tests/integration/test_idempotency.py assessment/tests/integration/test_publish_guard.py -q`

Expected: FAIL with missing publisher module.

- [ ] **Step 4: Implement deterministic keys and body markers**

```python
# assessment/ai_reviewer/publisher.py
import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from .contracts import Finding, ReviewRequest


class StaleHeadError(RuntimeError):
    """Raised when publication would target an obsolete pull request head."""


class PublisherAdapter(Protocol):
    def current_head_sha(self) -> str: ...
    def existing_review_bodies(self) -> tuple[str, ...]: ...
    def create_review(self, head_sha: str, comments: list[dict[str, object]]) -> None: ...


@dataclass(frozen=True, slots=True)
class PublishReport:
    published: int
    skipped: int


def _digest(value: dict[str, object]) -> str:
    payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_key(repository: str, pr_number: int, head_sha: str) -> str:
    return _digest({"repository": repository, "pr_number": pr_number, "head_sha": head_sha})


def finding_fingerprint(request: ReviewRequest, finding: Finding) -> str:
    return _digest(
        {
            "repository": request.repository,
            "pr_number": request.pr_number,
            "head_sha": request.head_sha,
            "path": finding.path,
            "line": finding.line,
            "category": finding.category,
            "source": finding.source,
            "title": " ".join(finding.title.split()).casefold(),
        }
    )


def marker(fingerprint: str) -> str:
    return f"<!-- ai-review:{fingerprint} -->"


def render_body(request: ReviewRequest, finding: Finding) -> str:
    evidence = "\n".join(f"- {item}" for item in finding.evidence)
    fingerprint = finding_fingerprint(request, finding)
    return (
        f"**{finding.severity.upper()} · {finding.category}**: {finding.title}\n\n"
        f"Impact: {finding.impact}\n\nEvidence:\n{evidence}\n\n"
        f"Suggestion: {finding.suggestion}\n\n{marker(fingerprint)}"
    )
```

- [ ] **Step 5: Implement `publish_findings()`**

Append this implementation to `publisher.py`:

```python
def publish_findings(
    adapter: PublisherAdapter,
    request: ReviewRequest,
    findings: tuple[Finding, ...],
) -> PublishReport:
    if adapter.current_head_sha() != request.head_sha:
        raise StaleHeadError("pull request head changed before publication")

    existing = adapter.existing_review_bodies()
    comments: list[dict[str, object]] = []
    expected_markers: list[str] = []
    skipped = 0
    for finding in findings[:8]:
        item_marker = marker(finding_fingerprint(request, finding))
        if any(item_marker in body for body in existing):
            skipped += 1
            continue
        expected_markers.append(item_marker)
        comments.append(
            {
                "path": finding.path,
                "line": finding.line,
                "side": "RIGHT",
                "body": render_body(request, finding),
            }
        )

    if not comments:
        return PublishReport(published=0, skipped=skipped)

    try:
        adapter.create_review(request.head_sha, comments)
    except Exception:
        reconciled = adapter.existing_review_bodies()
        if all(any(item_marker in body for body in reconciled) for item_marker in expected_markers):
            return PublishReport(published=len(comments), skipped=skipped)
        raise
    return PublishReport(published=len(comments), skipped=skipped)
```

- [ ] **Step 6: Run and commit**

Expected: publisher, guard, and idempotency tests pass.

```powershell
git add assessment/ai_reviewer/publisher.py assessment/tests/integration/test_idempotency.py assessment/tests/integration/test_publish_guard.py
git commit -m "feat: publish idempotent inline reviews"
```

## Task 6: Save Atomic Redacted Run Artifacts

**Files:**
- Create: `assessment/ai_reviewer/run_artifact.py`
- Test: `assessment/tests/integration/test_run_artifact.py`

- [ ] **Step 1: Write failing artifact tests**

Build a result containing `Authorization: Bearer secret`, `GLM_API_KEY=secret`, a PAT-shaped token, and Windows/POSIX absolute paths inside errors and trace values. Assert the output contains required fields, contains `[REDACTED]`, contains none of the secrets or local paths, limits errors to 20 entries of 500 characters, and leaves no temporary file after atomic replacement.

- [ ] **Step 2: Verify failure**

Run: `.venv\Scripts\python.exe -m pytest assessment/tests/integration/test_run_artifact.py -q`

Expected: FAIL with missing artifact module.

- [ ] **Step 3: Implement recursive redaction and atomic save**

```python
# assessment/ai_reviewer/run_artifact.py
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .contracts import ReviewResult

REDACTED = "[REDACTED]"
SENSITIVE_KEY = re.compile(r"api[_-]?key|token|authorization|cookie|password|credential|secret", re.IGNORECASE)
SENSITIVE_TEXT = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(\S+)"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|cookie|secret)\s*[:=]\s*)(\S+)"),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]+\b"),
    re.compile(r"\b[A-Za-z]:\\(?:Users|Documents|Temp)\\[^\s\"']+"),
    re.compile(r"/(?:home|Users|tmp)/[^\s\"']+"),
)


def _redact_text(value: str) -> str:
    result = value
    for pattern in SENSITIVE_TEXT:
        if pattern.groups >= 2:
            result = pattern.sub(lambda match: f"{match.group(1)}{REDACTED}", result)
        else:
            result = pattern.sub(REDACTED, result)
    return result


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): REDACTED if SENSITIVE_KEY.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def save_artifact(result: ReviewResult, directory: str | Path) -> Path:
    target_directory = Path(directory)
    target_directory.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    payload["errors"] = [_redact_text(error[:500]) for error in result.errors[:20]]
    payload = _redact(payload)
    target = target_directory / f"{result.run_id}.json"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{result.run_id}-", suffix=".tmp", dir=target_directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return target
```

- [ ] **Step 4: Run and commit**

Expected: artifact tests pass.

```powershell
git add assessment/ai_reviewer/run_artifact.py assessment/tests/integration/test_run_artifact.py
git commit -m "feat: save redacted run artifacts"
```

## Task 7: Orchestrate Probe and Full Review Modes

**Files:**
- Create: `assessment/ai_reviewer/cli.py`
- Test: `assessment/tests/integration/test_cli.py`

- [ ] **Step 1: Write failing dependency-injected orchestration tests**

Use fake event loader, adapter, static scanner, Agent reviewer, publisher, clock, and artifact writer. Cover success, static failure with valid Agent findings, Agent failure with valid static findings, both failures, timeout, artifact write from `finally`, and explicit probe mode selecting the first changed line without importing member modules.

- [ ] **Step 2: Verify failure**

Run: `.venv\Scripts\python.exe -m pytest assessment/tests/integration/test_cli.py -q`

Expected: FAIL with missing CLI module.

- [ ] **Step 3: Implement `PipelineDependencies` and `run_pipeline()`**

Use these exact member interfaces and orchestration states:

```python
# assessment/ai_reviewer/cli.py
from __future__ import annotations

import argparse
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .budget import Budget, BudgetExpired
from .contracts import Finding, ReviewRequest, ReviewResult
from .diff_scope import filter_publishable, parse_changed_scope
from .github_runtime import PullRequestEvent, load_event
from .publisher import PublishReport, publish_findings
from .run_artifact import save_artifact
from .upstream_adapter import UpstreamAdapter

StaticScan = Callable[[ReviewRequest, Budget], list[Finding]]
AgentReview = Callable[[ReviewRequest, list[Finding], Budget], tuple[list[Finding], list[dict]]]


@dataclass(frozen=True, slots=True)
class PipelineDependencies:
    event_loader: Callable[[str | Path], PullRequestEvent] = load_event
    adapter_factory: Callable[[PullRequestEvent, str], UpstreamAdapter] = UpstreamAdapter.from_token
    publisher: Callable[[UpstreamAdapter, ReviewRequest, tuple[Finding, ...]], PublishReport] = publish_findings
    artifact_saver: Callable[[ReviewResult, str | Path], Path] = save_artifact
    monotonic: Callable[[], float] = time.monotonic


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _error(stage: str, error: Exception) -> str:
    return f"{stage}: {type(error).__name__}: {str(error)[:300]}"


def _probe_finding(request: ReviewRequest) -> Finding:
    for path in request.changed_files:
        lines = sorted(request.changed_lines.get(path, ()))
        if lines:
            return Finding(
                path=path,
                line=lines[0],
                category="static",
                severity="low",
                confidence=1.0,
                title="AI review pipeline probe",
                evidence=("The controlled workflow reached the verified publication stage.",),
                impact="This synthetic finding validates the integration path only.",
                suggestion="Switch AI_REVIEW_MODE to full after Agent and static modules pass contract tests.",
                source="syntax",
            )
    raise ValueError("probe requires at least one added head line")


def run_pipeline(
    *,
    event_path: str | Path,
    artifact_dir: str | Path,
    token: str,
    model: str,
    mode: str,
    static_scan: StaticScan | None,
    agent_review: AgentReview | None,
    dependencies: PipelineDependencies = PipelineDependencies(),
) -> ReviewResult:
    started_at = _utc_now()
    started_monotonic = dependencies.monotonic()
    run_id = str(uuid.uuid4())
    errors: list[str] = []
    trace: list[dict] = []
    findings: list[Finding] = []
    request: ReviewRequest | None = None
    adapter: UpstreamAdapter | None = None
    status = "success"
    budget = Budget(limit_seconds=540.0, clock=dependencies.monotonic)

    try:
        event = dependencies.event_loader(event_path)
        if not event.same_repository:
            raise ValueError("external fork pull requests are not enabled")
        adapter = dependencies.adapter_factory(event, token)
        diff = adapter.unified_diff()
        changed_files, changed_lines = parse_changed_scope(diff)
        request = ReviewRequest(
            repository=event.repository,
            pr_number=event.pr_number,
            base_sha=event.base_sha,
            head_sha=event.head_sha,
            title=event.title,
            body=event.body,
            diff=diff,
            changed_lines=changed_lines,
            changed_files=changed_files,
        )
        trace.append({"stage": "analyze", "status": "completed"})

        static_findings: list[Finding] = []
        if mode == "probe":
            findings.append(_probe_finding(request))
            trace.append({"stage": "review", "status": "completed", "mode": "probe"})
        elif mode == "full":
            if static_scan is None or agent_review is None:
                raise RuntimeError("full mode requires static and Agent modules")
            try:
                budget.require_start("static scan")
                static_findings = list(static_scan(request, budget))
                findings.extend(static_findings)
            except BudgetExpired:
                raise
            except Exception as error:
                status = "failed"
                errors.append(_error("static", error))
            try:
                budget.require_start("Agent review")
                agent_findings, agent_trace = agent_review(request, static_findings, budget)
                findings.extend(agent_findings)
                trace.extend(dict(item) for item in agent_trace)
            except BudgetExpired:
                raise
            except Exception as error:
                status = "failed"
                errors.append(_error("agent", error))
        else:
            raise ValueError(f"unsupported mode: {mode}")
    except BudgetExpired as error:
        status = "timeout"
        errors.append(_error("budget", error))
    except Exception as error:
        status = "failed"
        errors.append(_error("pipeline", error))

    publishable: tuple[Finding, ...] = ()
    if request is not None:
        publishable = filter_publishable(findings, dict(request.changed_lines))
        trace.append({"stage": "verify", "status": "completed", "publishable_count": len(publishable)})
    if adapter is not None and request is not None and publishable:
        try:
            dependencies.publisher(adapter, request, publishable)
        except Exception as error:
            status = "failed"
            errors.append(_error("publisher", error))

    finished_at = _utc_now()
    result = ReviewResult(
        run_id=run_id,
        model=model,
        status=status,
        started_at=started_at.isoformat().replace("+00:00", "Z"),
        finished_at=finished_at.isoformat().replace("+00:00", "Z"),
        duration_seconds=max(0.0, dependencies.monotonic() - started_monotonic),
        findings=publishable,
        errors=tuple(errors),
        trace_summary=tuple(trace),
    )
    dependencies.artifact_saver(result, artifact_dir)
    return result
```

- [ ] **Step 4: Implement explicit modes**

Append the production entry point. Probe never imports member modules; full mode imports the frozen interfaces lazily.

```python
def _full_modules() -> tuple[StaticScan, AgentReview]:
    from .agent_loop import review
    from .static_scan import scan

    return scan, review


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--mode", choices=("probe", "full"), default="full")
    arguments = parser.parse_args()

    static_scan: StaticScan | None = None
    agent_review: AgentReview | None = None
    if arguments.mode == "full":
        try:
            static_scan, agent_review = _full_modules()
        except ImportError:
            # run_pipeline records the missing frozen interfaces in the artifact.
            static_scan = None
            agent_review = None

    result = run_pipeline(
        event_path=arguments.event,
        artifact_dir=arguments.artifact_dir,
        token=os.environ.get("GITHUB_TOKEN", ""),
        model=os.environ.get("AI_REVIEW_MODEL", "glm-5.2"),
        mode=arguments.mode,
        static_scan=static_scan,
        agent_review=agent_review,
    )
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run and commit**

Expected: CLI tests pass.

```powershell
git add assessment/ai_reviewer/cli.py assessment/tests/integration/test_cli.py
git commit -m "feat: orchestrate review pipeline"
```

## Task 8: Add the Controlled GitHub Action

**Files:**
- Create: `.github/workflows/ai-review.yml`
- Create: `assessment/tests/integration/test_workflow.py`

- [ ] **Step 1: Write failing workflow policy tests**

Parse the YAML text and assert it contains only `contents: read` and `pull-requests: write`, handles `opened/reopened/synchronize`, uses `timeout-minutes: 10`, Python 3.12, `persist-credentials: false`, an internal-head guard, `GLM_API_KEY` only on the guarded step, and artifact upload under `if: always()`. Assert it contains no `pull_request_target`, `permissions: write-all`, target test execution, or target dependency installation.

- [ ] **Step 2: Verify failure**

Run: `.venv\Scripts\python.exe -m pytest assessment/tests/integration/test_workflow.py -q`

Expected: FAIL because `.github/workflows/ai-review.yml` is absent.

- [ ] **Step 3: Add the pinned workflow**

Use this complete workflow. The SHAs match the repository's pinned checkout/setup actions and upload-artifact v4.6.2.

```yaml
name: AI Review Assessment

on:
  pull_request:
    types: [opened, reopened, synchronize]

permissions:
  contents: read
  pull-requests: write

concurrency:
  group: ai-review-${{ github.repository }}-${{ github.event.pull_request.number }}-${{ github.event.pull_request.head.sha }}
  cancel-in-progress: false

jobs:
  review:
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Check out controlled pull request
        uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
        with:
          persist-credentials: false

      - name: Set up Python
        uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0
        with:
          python-version: "3.12"
          cache: pip

      - name: Install reviewer dependencies
        run: python -m pip install --requirement requirements.txt --requirement requirements-dev.txt

      - name: Run controlled review
        env:
          AI_REVIEW_MODE: ${{ vars.AI_REVIEW_MODE || 'probe' }}
          AI_REVIEW_MODEL: glm-5.2
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          GLM_API_KEY: ${{ secrets.GLM_API_KEY }}
          PYTHONPATH: .
        run: >-
          python -m assessment.ai_reviewer.cli
          --event "$GITHUB_EVENT_PATH"
          --artifact-dir "$RUNNER_TEMP/ai-review"
          --mode "$AI_REVIEW_MODE"

      - name: Upload run artifact
        if: always()
        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        with:
          name: ai-review-${{ github.event.pull_request.number }}-${{ github.event.pull_request.head.sha }}
          path: ${{ runner.temp }}/ai-review/*.json
          if-no-files-found: error
          retention-days: 14
```

Switch the repository variable to `full` only after member A/B integration. The job-level internal-head guard prevents the secret-backed step from running on external fork pull requests.

- [ ] **Step 4: Validate YAML, run tests, and commit**

Run:

```powershell
.venv\Scripts\python.exe -c "import pathlib, yaml; yaml.safe_load(pathlib.Path('.github/workflows/ai-review.yml').read_text(encoding='utf-8'))"
.venv\Scripts\python.exe -m pytest assessment/tests/integration/test_workflow.py -q
```

Expected: YAML parses and workflow tests pass.

```powershell
git add .github/workflows/ai-review.yml assessment/tests/integration/test_workflow.py
git commit -m "feat: run controlled AI review workflow"
```

## Task 9: Verify, Push, and Open the Core Integration PR

**Files:**
- Modify only files found defective by focused verification.
- Do not modify member-owned `model_adapter.py`, `agent_loop.py`, `context_tools.py`, `verifier.py`, `static_scan.py`, rules, benchmark, or delivery docs.

- [ ] **Step 1: Run the complete focused suite**

```powershell
$env:PYTHONPATH = "."
.venv\Scripts\python.exe -m pytest assessment/tests/integration -q
```

Expected: all integration tests pass with zero real GitHub writes.

- [ ] **Step 2: Run focused static checks**

```powershell
.venv\Scripts\python.exe -m ruff check assessment
.venv\Scripts\python.exe -m flake8 assessment
git diff main...HEAD --check
```

Expected: all commands exit zero.

- [ ] **Step 3: Scan the branch for secrets and personal paths**

Run targeted searches for `GLM_API_KEY=`, `ghp_`, `github_pat_`, `Authorization:`, `Bearer `, and local absolute path prefixes. Expected: only deliberate redaction test strings are present; no real value or personal path appears.

- [ ] **Step 4: Push and open the focused pull request**

```powershell
git push origin feat/core-integration
```

Open a PR to `main` with this evidence structure:

```text
目标：完成组长负责的 PR 主链、发布守卫、预算、幂等和运行记录
改动：assessment 核心包、共享 mock、集成测试和 ai-review workflow
验证：列出 pytest、ruff、flake8、YAML 校验的真实结果
证据：共享契约提交 SHA、脱敏 JSON、受控 probe PR 与行级评论链接
限制：full 模式等待成员 A/B 模块及 GLM secret
接口影响：首次冻结 ReviewRequest/Finding/ReviewResult，无后续漂移
安全检查：无 Key、PAT、个人路径和 benchmark ground truth
```

- [ ] **Step 5: Configure the repository controls without exposing credentials**

In GitHub repository settings:

1. Create a `main` ruleset that requires pull requests, blocks force pushes, and blocks branch deletion. Do not require an additional approval that would prevent the sole merger from completing the sprint.
2. Invite the three exact GitHub usernames supplied by the team as `Write` collaborators. If the usernames have not been supplied, record this as an external prerequisite rather than inventing accounts.
3. Ask the repository owner to enter `GLM_API_KEY` directly into the Actions Secret form. Never request or echo the value in chat, shell history, logs, screenshots, or files.
4. Create repository variable `AI_REVIEW_MODE=probe`. Change it to `full` only after members A and B pass the frozen fixture contract.

Verify the ruleset and variable names in the GitHub UI. Verify only that the secret name exists; never reveal its value.

- [ ] **Step 6: Run the controlled probe before merge**

Set repository variable `AI_REVIEW_MODE=probe`, create an internal branch PR with one added line, and confirm Action posts exactly one marker-bearing inline comment on the current head. Re-run the workflow and confirm no duplicate comment. Save the run JSON and total duration.

- [ ] **Step 7: Merge only after live evidence passes**

Squash merge the core PR. Do not enable `full` mode until member A/B modules pass the same frozen fixture contract. After their integration, set `AI_REVIEW_MODE=full`, run one benchmark PR, and record the real comment and artifact links before tagging any release.
