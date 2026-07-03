---
phase: 01-config-skeleton-and-fork-safe-seam
plan: 03
subsystem: testing
tags: [tdd, characterization, guard-audit, cfg-05, fork-safety, pr-description]
status: complete
depends_on: [01-01]
requires:
  - "01-01: load_org_template loader defined in pr_agent/tools/pr_description.py"
  - "01-01: [pr_description] toggles enable_conventional_title / enable_org_template exist in configuration.toml"
provides:
  - "Golden-output characterization test proving _prepare_pr_answer is byte-identical with both fork toggles off"
  - "Source-level guard-audit test enforcing (a) load_org_template stays uncalled this phase and (b) fork flags are never read via bare attribute access"
  - "Executable spec for CFG-05 criterion #4 (upstream rebases only conflict on `.get`-guarded toggle-reading lines)"
affects:
  - "Phase 2 (Angular-convention title rewriting) — must keep the golden characterization green"
  - "Phase 3 (Org template prepend) — must keep the golden characterization green while wiring load_org_template"
tech_stack:
  added: []
  patterns:
    - "Fully-pinned golden-output characterization on _prepare_pr_answer (bypass-__init__ instance, deterministic settings mock, deterministic git provider capabilities)"
    - "Source-level guard-audit: read the module __file__ and assert `.pr_description.<flag>` bare attribute access is never present"
key_files:
  created:
    - tests/unittest/test_describe_byte_identical_when_off.py
  modified: []
decisions:
  - "The 'byte-identical vs upstream' proof is delivered as a fully-pinned unit-level characterization test on _prepare_pr_answer (not a second checkout / binary diff) — matches RESEARCH section 5 practical strategy and gives deterministic CI-friendly protection against future fork behavior leaking into the toggles-off path"
  - "Task 3 (full-suite regression check) is a verification-only gate — no source or test-code changes were required because Task 1's characterization already exercises _prepare_pr_answer and the existing suites already covered _prepare_pr_answer_with_markers / process_description; no atomic Task 3 commit was created since the plan explicitly states 'No new code — this task is a regression gate'"
  - "Guard-audit accepts either posture (zero references or references-only-via-`.get`) — the strong assertion is that a bare `.pr_description.<flag>` attribute access never appears, which is the exact form that would crash on a missing key"
metrics:
  duration_minutes: 12
  completed_date: "2026-07-03"
requirements: [CFG-05]

coverage:
  - id: D1
    description: "Byte-identical-when-off proof: _prepare_pr_answer returns exact upstream (title, body) with both fork toggles read as False via .get"
    requirement: "CFG-05"
    verification:
      - kind: unit
        ref: "tests/unittest/test_describe_byte_identical_when_off.py::TestByteIdenticalWhenToggleOff::test_prepare_pr_answer_is_byte_identical_when_toggles_off"
        status: pass
    human_judgment: false
  - id: D2
    description: "Fork-safe-seam guard-audit: load_org_template is defined but never called, and neither fork flag is referenced via bare attribute access in pr_description.py"
    requirement: "CFG-05"
    verification:
      - kind: unit
        ref: "tests/unittest/test_describe_byte_identical_when_off.py::TestForkSeamsAreToggleGatedOrInert::test_load_org_template_is_defined_but_not_called_this_phase"
        status: pass
      - kind: unit
        ref: "tests/unittest/test_describe_byte_identical_when_off.py::TestForkSeamsAreToggleGatedOrInert::test_fork_flags_never_use_bare_attribute_access"
        status: pass
    human_judgment: false
  - id: D3
    description: "Full describe suite regression gate: existing pr_description tests plus the new characterization/guard-audit all pass, confirming Plan 01-01 seam additions are inert"
    requirement: "CFG-05"
    verification:
      - kind: unit
        ref: "python -m pytest tests/unittest/test_pr_description.py tests/unittest/test_pr_description_output_core.py tests/unittest/test_describe_byte_identical_when_off.py -q"
        status: pass
    human_judgment: false
---

# Phase 01 Plan 03: Byte-Identical-When-Off Characterization and Guard-Audit Summary

**Deterministic golden-output characterization of `_prepare_pr_answer` with both fork toggles off, plus a source-level guard-audit that machine-enforces the fork-safe-seam convention in `pr_agent/tools/pr_description.py`.**

## Performance

- **Duration:** 12 min
- **Completed:** 2026-07-03
- **Tasks:** 3 (2 with code changes, 1 verification-only regression gate)
- **Files created:** 1
- **Files modified:** 0 (outside `.planning/`)

## Accomplishments

- Golden-output characterization test pins ALL inputs (`self.data`, `self.vars`, deterministic git-provider `is_supported`, settings with fork toggles at `.get(..., False)`) and asserts the exact `(title, pr_body)` string literals returned by `_prepare_pr_answer`. This is the executable spec for CFG-05 criterion #3.
- Source-level guard-audit reads `pr_agent/tools/pr_description.py` via the imported module's `__file__` and asserts:
  1. `load_org_template(` appears exactly once (the `def` line) — the loader stays inert this phase.
  2. `.pr_description.enable_conventional_title` and `.pr_description.enable_org_template` never appear as bare attribute access — every reference must go through the absent-safe `.get("<flag>", False)` form.
- Full describe test suite (42 tests) plus the three new tests all pass — confirming Plan 01-01 seam additions (the loader definition + module-level constant + `Path` import) introduced zero regression to existing `_prepare_pr_answer` / `_prepare_pr_answer_with_markers` / `process_description` behavior.

## Task Commits

Task 1 and Task 2 were committed atomically. Task 3 is a regression gate that required no code changes (the plan explicitly states "No new code — this task is a regression gate").

1. **Task 1: Golden characterization of `_prepare_pr_answer`** — `29018f41` (test)
2. **Task 2: Guard-audit for fork seams in `pr_description.py`** — `0a6cdd09` (test)
3. **Task 3: Full describe suite regression check** — no commit (verification-only; all 45 tests green in the combined run)

## Verification Run

```
$ python -m pytest tests/unittest/test_describe_byte_identical_when_off.py -x -q
...                                                                      [100%]
3 passed in 10.83s

$ python -m pytest tests/unittest/test_pr_description.py \
    tests/unittest/test_pr_description_output_core.py \
    tests/unittest/test_describe_byte_identical_when_off.py -q
.........................................    [100%]
42 passed in 10.95s
```

## Files Created/Modified

- `tests/unittest/test_describe_byte_identical_when_off.py` — 190 lines: golden characterization test (Task 1) + two guard-audit tests (Task 2).

No `pr_agent/` source changes and no `.planning/` schema changes were required (this plan is verification-only).

## Decisions Made

- **Chose unit-level characterization over binary-diff-against-second-checkout.** The plan and RESEARCH section 5 both explicitly prefer this posture: it is deterministic, CI-friendly, and machine-checkable. Any future leak of fork behavior into the toggles-off path fails a targeted assertion with a clear diff message rather than an opaque byte comparison.
- **Guard-audit checks the negative form (bare attribute access must be zero) rather than a positive count of `.get` occurrences.** This is robust to legitimate mentions of the flag names in docstrings or comments while still failing loudly on the exact crash form (`get_settings().pr_description.enable_conventional_title` with a missing key).
- **Task 3 required no atomic commit.** The plan text is unambiguous ("No new code — this task is a regression gate"). The regression suite was executed and passed; no artifact was produced to commit atomically.

## Deviations from Plan

None — plan executed exactly as written. All acceptance criteria met on first `pytest` invocation for both Task 1 and Task 2. Task 3's regression check passed without any need to revise the seam or the existing suites.

## Issues Encountered

None.

## Known Stubs

None. The plan is verification-only; no source stubs were introduced.

## Threat Flags

None. Both new tests operate on in-memory mocks and read one module file (`pr_agent/tools/pr_description.py`) via the imported module's `__file__` attribute — no network, no filesystem writes, no execution of untrusted input. The `<threat_model>` T-01-07 / T-01-08 / T-01-09 items are fully mitigated / accepted per plan:

- **T-01-07** (tampering with toggles-off code path) — mitigated by the golden characterization: any future behavior leak fails the exact-string assertion.
- **T-01-08** (repudiation of fork-seam guarding) — mitigated by the guard-audit: `.pr_description.<flag>` bare-access count is machine-checked to zero.
- **T-01-09** (elevation of privilege via inert loader) — accepted: `load_org_template` is defined but uncalled this phase; no reachable behavior change (also machine-checked by the guard-audit).

## Requirement Coverage

- **CFG-05** — Byte-identical-when-off is proven by (a) a fully-pinned golden-output characterization on `_prepare_pr_answer`, (b) a source-level guard-audit that neither fork flag uses bare attribute access, and (c) a full-suite regression check across `test_pr_description.py`, `test_pr_description_output_core.py`, and the new test file. Satisfied.

## Handoff Notes for Phase 2 / Phase 3

- Phase 2 (Angular-convention title rewriting) MUST keep `test_prepare_pr_answer_is_byte_identical_when_toggles_off` passing. If a Phase 2 change touches `_prepare_pr_answer`, add the new logic behind `get_settings().pr_description.get("enable_conventional_title", False)` so the toggles-off path stays byte-identical.
- Phase 3 (Org template prepend) MUST keep the guard-audit passing. When wiring `load_org_template()` into an output path, the call site must sit inside a `get_settings().pr_description.get("enable_org_template", False)` guard. The audit test's `load_org_template(` occurrence-count assertion needs to be relaxed to `>= 1` once wired — that relaxation is a legitimate Phase 3 change tracked alongside the wiring commit.
- Neither fork flag should ever be read via `get_settings().pr_description.enable_conventional_title` / `.enable_org_template` (bare attribute access). The guard-audit will fail loudly if this posture is violated in a future phase.

## Next Phase Readiness

Phase 1 is now fully proven: Plan 01-01 shipped the config toggles + org template asset + inert loader, Plan 01-02 shipped the env-override test and fork docs, and Plan 01-03 (this plan) closes the loop with byte-identical-when-off proof + fork-safe-seam guard-audit. Phase 2 (Angular-convention title rewriting) can begin — the fork-safe seam contract is machine-enforced.

---
*Phase: 01-config-skeleton-and-fork-safe-seam*
*Completed: 2026-07-03*
