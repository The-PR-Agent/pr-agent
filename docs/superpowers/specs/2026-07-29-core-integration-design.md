# Core Integration Design

Date: 2026-07-29
Repository: `JiXia830/pr-agent`
Branch: `feat/core-integration`
Owner: team lead / core integration

## 1. Objective

Build the assessment-only control plane that turns a controlled GitHub pull request into verified inline review
comments and a redacted run artifact. The implementation stays under `assessment/` plus one dedicated workflow so that
the upstream PR-Agent code remains easy to update.

The core integration owns event parsing, diff scope, orchestration, deterministic publication guards, time budget,
idempotency, and run records. The Agent, model, static-analysis, benchmark, and delivery modules remain owned by the
other team members and connect through the frozen contracts below.

This is an assessment/private-preview release. It only comments on controlled internal pull requests. It does not
execute repository code, apply fixes, push commits, block merges, or claim production readiness.

## 2. Selected Approach

Use an isolated `assessment.ai_reviewer` package with a thin adapter around upstream PR-Agent capabilities.

This approach was selected over:

- modifying PR-Agent providers and tools directly, which would couple the sprint work to unstable upstream internals;
- building a completely standalone reviewer, which would not preserve the required PR-Agent integration path.

The package boundary keeps the sprint interfaces stable while allowing `upstream_adapter.py` to absorb upstream
changes. No member module may call GitHub publication APIs directly.

## 3. Frozen Contracts

`assessment/ai_reviewer/contracts.py` defines validated dataclasses or equivalent immutable value objects.

### ReviewRequest

- `repository: str`
- `pr_number: int`
- `base_sha: str`
- `head_sha: str`
- `title: str`
- `body: str`
- `diff: str`
- `changed_lines: dict[str, set[int]]`
- `changed_files: list[str]`

### Finding

- `path: str`
- `line: int`
- `category: static | business_logic | logic | memory | security | architecture`
- `severity: low | medium | high | critical`
- `confidence: float`, inclusive range `0.0..1.0`
- `title: str`
- `evidence: list[str]`
- `impact: str`
- `suggestion: str`
- `source: agent | semgrep | syntax`

### ReviewResult

- `run_id: str`
- `model: str`
- `status: success | failed | timeout`
- `started_at: str`
- `finished_at: str`
- `duration_seconds: float`
- `findings: list[Finding]`
- `errors: list[str]`
- `trace_summary: list[dict]`

Constructors reject missing required fields, unknown enum values, non-finite confidence values, invalid timestamps,
and mutable aliases that could change a request after validation. Serialization produces JSON-compatible values and
sorts sets deterministically.

## 4. Components

### `github_runtime.py`

Read `GITHUB_EVENT_PATH` and accept only `pull_request` events with actions `opened`, `reopened`, or `synchronize`.
Extract repository, pull request number, base SHA, head SHA, title, and body. Reject missing or malformed fields before
any model or publication call.

The runtime verifies that the pull request head repository equals the target repository before exposing secrets. This
release does not process automatic secret-backed reviews from external forks.

### `upstream_adapter.py`

Expose a small stable interface for obtaining PR metadata, unified diff, and repository file content through existing
PR-Agent provider capabilities where practical. Upstream classes and configuration never leak into the shared
contracts. Adapter failures are normalized to typed core errors with redacted messages.

### `diff_scope.py`

Parse unified diff file headers and hunks, including additions, modifications, deletions, renames, and paths containing
spaces. Track only new-file line numbers from the `+new_start,new_count` side. Deleted-only files have no publishable
head lines. Normalize paths to repository-relative POSIX form and reject traversal, absolute paths, and ambiguous
duplicate headers.

`filter_publishable()` accepts findings only when:

- the path is in `changed_files`;
- the line is in `changed_lines[path]`;
- category, severity, confidence, evidence, impact, and suggestion pass deterministic validation.

It sorts by severity (`critical`, `high`, `medium`, `low`), then confidence descending, then path and line, and returns
at most eight findings.

### `budget.py`

Use a monotonic clock and a 540-second review deadline. Every potentially slow boundary checks the remaining budget
before starting work. Once the deadline expires, no new context request, static scan, or model request may start.
Publication and artifact cleanup use the reserved final 60 seconds inside the workflow's 10-minute job limit.

Timeout is a terminal `timeout` result, never `success` or `partial`.

### `publisher.py`

This is the only module allowed to write pull request comments. It receives already-filtered findings and publishes a
single GitHub review containing inline comments attached to the current `head_sha`.

Each comment includes a hidden marker derived from a canonical finding fingerprint:

`repository + pr_number + head_sha + path + line + category + source + normalized title`

Before publishing, the module lists existing review comments from the configured bot identity. A matching marker is
skipped. New findings on the same head may still be added. This makes retries safe without hiding newly discovered
issues. A run-level idempotency key derived from `repository + pr_number + head_sha` is stable and independently tested.

The publisher rechecks the current head SHA immediately before writing. A changed head aborts publication with a
failed result so stale comments cannot land on a newer revision.

### `run_artifact.py`

Serialize every terminal result to an atomic JSON file under a caller-provided artifact directory. The workflow uploads
that directory even after failure or timeout.

The serializer redacts case-insensitive keys and text patterns for API keys, tokens, authorization headers, cookies,
credentials, and local absolute paths. It stores finding evidence, not the full private source file or full prompt.
Errors are bounded in count and length.

### `cli.py`

The CLI is an orchestration layer only:

1. Parse and validate the event.
2. Obtain and parse the pull request diff.
3. Create `ReviewRequest`.
4. Invoke `static_scan.scan(request, budget)`.
5. Invoke `agent_loop.review(request, static_findings, budget)`.
6. Validate and filter all findings.
7. Reconcile the current head and publish non-duplicate comments.
8. Save the terminal `ReviewResult` in a `finally` path.

Member modules are imported behind narrow adapter functions. Integration tests inject fakes; the core branch does not
create or edit member-owned implementation files.

## 5. Workflow

`.github/workflows/ai-review.yml` handles internal `pull_request` events for `opened`, `reopened`, and `synchronize`.
It uses:

```yaml
permissions:
  contents: read
  pull-requests: write
```

The job has `timeout-minutes: 10`, uses Python 3.12, installs pinned project dependencies, runs the core CLI, and
uploads the run JSON with `if: always()`. Checkout uses `persist-credentials: false`. The job never runs target
repository scripts, tests, generated commands, or target dependency installation.

`GLM_API_KEY` is passed only for internal same-repository branches. Logs print the configured model identifier but
never secret values, authorization headers, full prompts, or full private source.

Benchmark repositories later use a thin workflow pinned to an exact central commit SHA. Floating `main` references
are not permitted in the final release.

## 6. Failure Handling

- Invalid event or contract: fail before analysis and save a redacted `failed` artifact.
- Diff parse ambiguity: fail closed; do not guess publishable lines.
- Static module failure: record the error and continue to Agent review only when the remaining budget permits. The
  terminal status remains `failed`, but valid Agent findings may still be published after deterministic validation.
- Agent/model failure: record the error and preserve valid static findings. The terminal status remains `failed`, but
  valid static findings may still be published after deterministic validation. If both modules fail, publish nothing.
- Head SHA changed: publish nothing and fail so the new `synchronize` event can perform a fresh run.
- GitHub publication response unknown: re-list marker-bearing comments before retrying; never replay blindly.
- Deadline reached: stop new work, save `timeout`, and attempt artifact upload.
- Artifact write failure: emit a short redacted stderr message and return non-zero.

Model output, repository contents, diff text, comments, and tool results are untrusted data. They cannot authorize new
tools, commands, paths, permissions, network destinations, or writes.

## 7. Test Strategy

All integration tests run without real GitHub writes or model calls:

- `test_diff_scope.py`: additions, modifications, deletions, renames, spaces, traversal rejection, and old/new line
  separation;
- `test_publish_guard.py`: changed-line enforcement, contract validation, priority ordering, eight-comment cap, and
  stale-head rejection;
- `test_idempotency.py`: stable run key, stable finding fingerprint, duplicate skip, and new-finding publication;
- `test_budget.py`: monotonic deadline and refusal to start new context after timeout;
- `test_run_artifact.py`: required fields, atomic output, deterministic serialization, bounded errors, and secret/path
  redaction;
- an orchestration test with fake static, Agent, GitHub, and clock adapters covering success, failure, and timeout.

The focused command is:

```bash
PYTHONPATH=. python -m pytest assessment/tests/integration -q
```

Before the branch is considered ready, run focused Ruff/flake8 checks on changed Python files and validate the workflow
YAML. Live publication is tested only on a controlled pull request after the branch tests pass.

## 8. Delivery Sequence

1. Commit this approved design.
2. Implement contracts and deterministic helpers test-first.
3. Implement GitHub runtime, upstream adapter, publisher, and artifact handling with mocked integration tests.
4. Implement CLI orchestration and workflow.
5. Push `feat/core-integration` and open a focused pull request with test and security evidence.
6. Configure the repository ruleset, collaborators, and `GLM_API_KEY` through GitHub settings without exposing values.
7. Integrate member A and B through the frozen contracts, then validate one controlled benchmark pull request.
8. Pin benchmark workflows to the final central commit and tag `v0.1.0-assessment` only after all release gates pass.

## 9. Acceptance Criteria

The core integration is accepted when:

- controlled pull request events automatically invoke the workflow;
- base/head SHA and changed head lines are correct;
- static and Agent modules are called only through the frozen contracts;
- every published finding targets a real changed line on the current head;
- same-head reruns do not create uncontrolled duplicate comments;
- every run produces a redacted JSON artifact;
- review expansion stops at 540 seconds and the workflow finishes within 600 seconds;
- logs and repository history contain no API key, PAT, authorization header, cookie, or private absolute path;
- all focused integration tests pass without real GitHub writes;
- after member integration, at least one controlled benchmark pull request produces a valid target finding.

Repository rules, collaborator invitations, the GLM secret value, member implementations, benchmark creation, and final
release evidence require their respective accounts or inputs; they cannot be simulated or reported as complete.
