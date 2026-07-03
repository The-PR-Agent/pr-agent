---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: planning
last_updated: "2026-07-03T02:12:49.599Z"
progress:
  total_phases: 3
  completed_phases: 1
  total_plans: 3
  completed_plans: 3
  percent: 33
current_phase: 01
current_phase_name: Config skeleton and fork-safe seam
---

# Project State

**Project:** PR-Agent — Org MR Enhancements
**Milestone:** v1.0
**Branch:** gsd/v1.0-milestone
**Mode:** mvp (Vertical MVP)

## Project Reference

**Core Value:** When a GitLab MR opens, `describe` produces a conventionally-formatted title and an org-standard description body (What/Risk AI-filled, checklist for the human) on top of the existing PR-Agent walkthrough — with zero manual formatting by the author.

**Current Focus:** Phase 01 — Config skeleton and fork-safe seam

## Current Position

**Phase:** 2
**Plan:** Not started
**Status:** Ready to plan
**Progress:** [███████░░░] 67%

**Next action:** Run `/gsd-plan-phase 1` to decompose Phase 1 into executable plans.

## Roadmap Snapshot

| Phase | Goal | Requirements | Status |
|-------|------|--------------|--------|
| 1. Config skeleton and fork-safe seam | Gated toggles default off (env-overridable via dynaconf), fork-owned template storage, byte-identical when off | CFG-01, CFG-02, CFG-03, CFG-05, CFG-06 | Not started |
| 2. Angular-convention title rewriting | Prompt-side title generation + Python validator/repair + safe fallback; auto-force publishing | CFG-04, TITLE-01, TITLE-02, TITLE-03, TITLE-04, TITLE-05, TITLE-06 | Not started |
| 3. Org template prepend with idempotency | AI-filled What/Risk prepended above PR-Agent's default output (unchanged), sentinel-bounded, checkbox-preserving on re-runs | TMPL-01..09 | Not started |

**Coverage:** 21/21 v1 requirements mapped (100%).

## Performance Metrics

_Populated as phases complete._

| Metric | Value |
|--------|-------|
| Phases complete | 0/3 |
| Plans complete | 0/0 |
| v1 requirements shipped | 0/21 |

## Accumulated Context

### Key Decisions (locked)

- Breaking-change `!` marker deferred to v2.
- PR-Agent's own default description output (including its `## PR Description` section) is retained unchanged when the org template is on — the org template is purely additive (prepended above); nothing is removed. Walkthrough retained.
- `enable_conventional_title=true` auto-forces AI title publishing (CFG-04).
- Both toggles are env-overridable via PR-Agent's existing dynaconf `env_loader` (e.g. `PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE=true`); no new plumbing required — `config_loader.py:12-18` already sets `envvar_prefix=False` + `merge_enabled=True`. Verify-and-document only (CFG-06).
- v1 idempotency is HTML-comment-sentinel based; per-section human-edit hashing deferred to v2.
- No inline edits to `pr_description_prompts.toml`; use `extra_instructions` / Jinja `{% if %}` blocks / fork-owned files.
- All new toggles default to `false`.

### Open Design Gates (resolve during planning)

- Empty `note_risk` handling: omit the section entirely, render as empty header, or render "None"? (Phase 3.)
- Exact insertion point for the prepended org template block — before the entire PR-Agent default output, or between the title-line and the `## PR Description` header? Success criterion #1 says "purely additive (prepended above)"; confirm the concrete assembly order in `_prepare_pr_answer`. (Phase 3.)

### Todos

_None yet._

### Blockers

_None._

## Session Continuity

**Last session:** 2026-07-03T01:04:41.992Z
**Files updated:**

- `.planning/ROADMAP.md` (revised: Phase 1 requirements/success criteria updated, Phase 3 success criterion #1 reversed, Locked Design Decisions table updated, coverage 21/21)
- `.planning/STATE.md` (revised: Roadmap Snapshot, Coverage, Performance Metrics, Key Decisions, Open Design Gates updated)
- `.planning/REQUIREMENTS.md` (already updated upstream: CFG-06 added, TMPL-05 reversed, traceability 21/21)

**Resume from:** `/gsd-plan-phase 1`

---
*State initialized: 2026-07-02*
*Revised: 2026-07-02*
