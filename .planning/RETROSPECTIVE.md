# Project Retrospective

*A living document updated after each milestone. Lessons feed forward into future planning.*

## Milestone: 1.0 — Org MR Enhancements

**Shipped:** 2026-07-03
**Phases:** 3 | **Plans:** 6 | **Sessions:** multiple interrupted/resumed sessions

### What Was Built

- Defaults-off `[pr_description]` toggles for conventional titles and org template prepend.
- Fork-owned org template asset and loader, without shared prompt TOML edits.
- Angular title validator, publish seam, prompt steering, and malformed-output fallback.
- Sentinel-wrapped org template prepend with AI-filled What/Risk sections and checkbox-state preservation.
- GitLab-only provider gating for both new feature toggles.

### What Worked

- Small vertical phases kept the fork seam, title behavior, and template behavior isolated.
- Golden defaults-off tests caught drift in upstream-compatible behavior.
- Source-level guard tests made rebase-sensitive fork seams explicit.
- Post-review fixes caught rerun and AI-forged sentinel risks before closeout.

### What Was Inefficient

- Phase 3 verification initially missed the GitLab-only scope implied by requirements.
- Planning metadata needed manual reconciliation after implementation was already complete.
- Lint verification could not run because `ruff` and `flake8` were unavailable in the active environment.

### Patterns Established

- Read fork flags with `.get("<flag>", False)` and keep feature helpers provider-aware.
- Steer fork-specific prompt behavior through runtime `extra_instructions`.
- Preserve default PR-Agent output by prepending or publishing at the final seam, not by rewriting shared prompt internals.
- Use sentinels for generated markdown blocks and preserve only explicit human-owned state.

### Key Lessons

1. Provider scope must be tested directly when requirements say "GitLab-only"; config gating alone is not enough.
2. Generated-description reruns need provider-level cleanup before "user description" detection.
3. AI-filled markdown must strip reserved sentinels before interpolation.
4. Keep milestone audit after final integration fixes, not before.

### Cost Observations

- Model mix: not measured.
- Sessions: multiple, with one handoff before final closeout.
- Notable: targeted test suites gave enough confidence without full repository test cost.

---

## Cross-Milestone Trends

### Process Evolution

| Milestone | Sessions | Phases | Key Change |
|-----------|----------|--------|------------|
| 1.0 | multiple | 3 | Added audit, validation, security, and retrospective closeout around a fork feature milestone. |

### Cumulative Quality

| Milestone | Tests | Coverage | Zero-Dep Additions |
|-----------|-------|----------|-------------------|
| 1.0 | 94 related tests passed at closeout | 21/21 requirements audited | 0 new dependencies |

### Top Lessons

1. Defaults-off fork behavior needs both behavioral and source-level tests.
2. Cross-provider negative tests belong in the first release of provider-scoped behavior.
