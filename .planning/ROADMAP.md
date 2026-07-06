# Roadmap: PR-Agent — Org MR Enhancements

## Milestones

- [x] **1.0 Org MR Enhancements** — shipped 2026-07-03; 3 phases, 6 plans, 21/21 requirements ([roadmap archive](milestones/1.0-ROADMAP.md), [requirements archive](milestones/1.0-REQUIREMENTS.md), [audit](milestones/1.0-MILESTONE-AUDIT.md))

## Completed

<details>
<summary>1.0 Org MR Enhancements — SHIPPED 2026-07-03</summary>

- [x] Phase 1: Config skeleton and fork-safe seam (3/3 plans) — completed 2026-07-03
- [x] Phase 2: Angular-convention title rewriting (2/2 plans) — completed 2026-07-03
- [x] Phase 3: Org template prepend with idempotency (1/1 plan) — completed 2026-07-03

Delivered GitLab-only, config-gated `describe` enhancements:

- Defaults-off `[pr_description]` toggles for conventional titles and org template prepend.
- Angular Commit Convention title normalization with safe fallback.
- Sentinel-wrapped org template prepend with AI-filled What/Risk sections, human checklist preservation, and PR-Agent walkthrough retained below.
- Non-GitLab providers ignore these toggles in v1.

</details>

## Next

No active milestone. Start the next planning cycle with `$gsd-new-milestone`.

### Phase 4: Expose v1.0 describe toggles via config__* env vars and embed pr_agent:walkthrough into org template

**Goal:** [To be planned]
**Requirements**: TBD
**Depends on:** Phase 3
**Plans:** 0 plans
Plans:

- [ ] TBD (run /gsd-plan-phase 4 to break down)

---
*Last updated: 2026-07-03 after 1.0 completion*
