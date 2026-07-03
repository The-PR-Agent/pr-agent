---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
current_phase: null
status: Awaiting next milestone
stopped_at: Milestone 1.0 complete and archived
last_updated: "2026-07-03T07:48:33.778Z"
last_activity: 2026-07-03
last_activity_desc: Milestone 1.0 completed and archived
progress:
  total_phases: 3
  completed_phases: 3
  total_plans: 6
  completed_plans: 6
  percent: 100
current_phase_name: null
---

# Project State

**Project:** PR-Agent — Org MR Enhancements
**Milestone:** v1.0
**Branch:** gsd/v1.0-milestone
**Mode:** mvp (Vertical MVP)

## Project Reference

**Core Value:** When a GitLab MR opens, `describe` produces a conventionally-formatted title and an org-standard description body (What/Risk AI-filled, checklist for the human) on top of the existing PR-Agent walkthrough — with zero manual formatting by the author.

**Current Focus:** Planning next milestone

## Current Position

Phase: Milestone 1.0 complete
Plan: —
Status: Awaiting next milestone
Last activity: 2026-07-03 — Milestone 1.0 completed and archived

## Roadmap Snapshot

| Phase | Goal | Requirements | Status |
|-------|------|--------------|--------|
| 1. Config skeleton and fork-safe seam | Gated toggles default off (env-overridable via dynaconf), fork-owned template storage, byte-identical when off | CFG-01, CFG-02, CFG-03, CFG-05, CFG-06 | Complete |
| 2. Angular-convention title rewriting | Prompt-side title generation + Python validator/repair + safe fallback; auto-force publishing | CFG-04, TITLE-01, TITLE-02, TITLE-03, TITLE-04, TITLE-05, TITLE-06 | Complete |
| 3. Org template prepend with idempotency | AI-filled What/Risk prepended above PR-Agent's default output (unchanged), sentinel-bounded, checkbox-preserving on re-runs | TMPL-01..09 | Complete |

**Coverage:** 21/21 v1 requirements shipped and audited (100%).

## Performance Metrics

_Populated as phases complete._

| Metric | Value |
|--------|-------|
| Phases complete | 3/3 |
| Plans complete | 6/6 |
| v1 requirements shipped | 21/21 |
| Phase 02 P01 | 8min | 2 tasks | 2 files |
| Phase 02 P02 | 10min | 2 tasks | 2 files |
| Phase 03 P01 | 35min | 2 tasks | 4 files |

## Accumulated Context

### Key Decisions (locked)

- Breaking-change `!` marker deferred to v2.
- PR-Agent's own default description output (including its `## PR Description` section) is retained unchanged when the org template is on — the org template is purely additive (prepended above); nothing is removed. Walkthrough retained.
- `enable_conventional_title=true` auto-forces AI title publishing (CFG-04).
- Both toggles are env-overridable via PR-Agent's existing dynaconf `env_loader` (e.g. `PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE=true`); no new plumbing required — `config_loader.py:12-18` already sets `envvar_prefix=False` + `merge_enabled=True`. Verify-and-document only (CFG-06).
- v1 idempotency is HTML-comment-sentinel based; per-section human-edit hashing deferred to v2.
- No inline edits to `pr_description_prompts.toml`; use `extra_instructions` / Jinja `{% if %}` blocks / fork-owned files.
- All new toggles default to `false`.

### Open Design Gates

_None._

### Todos

_None yet._

### Blockers

_None._

## Session Continuity

**Stopped at:** Milestone 1.0 complete and archived
**Resume file:** None

**Last session:** 2026-07-03T07:26:00Z
**Files updated:**

- `.planning/ROADMAP.md` (revised: Phase 1 requirements/success criteria updated, Phase 3 success criterion #1 reversed, Locked Design Decisions table updated, coverage 21/21)
- `.planning/STATE.md` (revised: Roadmap Snapshot, Coverage, Performance Metrics, Key Decisions, Open Design Gates updated)
- `.planning/REQUIREMENTS.md` (already updated upstream: CFG-06 added, TMPL-05 reversed, traceability 21/21)

**Resume from:** `/gsd-new-milestone`

---
*State initialized: 2026-07-02*
*Revised: 2026-07-02*

## Decisions

- [Phase 02]: `_normalize_angular_title` uses a strict final regex gate; malformed short output such as `feat: AB` falls back to `None`.
- [Phase 02]: Plan 02-02 kept _prepare_pr_answer and _prepare_pr_answer_with_markers title-selection blocks untouched; publish seam sources sanitized self.ai_title instead. — Plan prohibited editing inline title selection blocks and CFG-04 needs the AI title after those helpers consume self.data['title'].
- [Phase 02]: Plan 02-02 steered Angular title generation through runtime extra_instructions instead of editing pr_description_prompts.toml. — Project decision keeps prompt TOML byte-stable; extra_instructions is the supported runtime steering seam.
- [Phase 02]: Conventional-title steering is per PRDescription instance only; global settings.pr_description.extra_instructions is not mutated.
- [Phase 03]: Empty `note_risk` renders as `None` under a stable Note / Risk header.
- [Phase 03]: Org template is prepended above the entire PR-Agent generated body, then old sentinel blocks are stripped from rerun bodies to avoid duplication.
- [Phase 03]: Existing MR description is read through `git_provider.get_pr_description(full=True)` to preserve checklist tick states by matching checklist label.

## Operator Next Steps

- Start the next milestone with /gsd-new-milestone
