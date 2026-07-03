# Milestones

## 1.0 Org MR Enhancements (Shipped: 2026-07-03)

**Delivered:** GitLab-only `describe` enhancements for Angular MR titles and org-template description prepend, gated behind defaults-off `[pr_description]` toggles.

**Phases completed:** 3 phases, 6 plans, 14 tasks

**Key accomplishments:**

- Added defaults-off config toggles and env-var override documentation for fork-owned `describe` behavior.
- Kept toggles-off output byte-identical to upstream behavior with golden characterization coverage.
- Added Angular title normalization, malformed-title fallback, and publish-seam coverage.
- Added sentinel-wrapped org template prepend with AI-filled What/Risk sections and human checkbox preservation.
- Preserved PR-Agent's generated walkthrough below the org template.
- Limited both new behaviors to GitLab providers; non-GitLab providers ignore the toggles.

**Verification:**

- Milestone audit: `.planning/milestones/1.0-MILESTONE-AUDIT.md` — passed.
- Latest related suite: 94 passed.
- Latest focused GitLab-only feature suite: 37 passed.

**Archives:**

- `.planning/milestones/1.0-ROADMAP.md`
- `.planning/milestones/1.0-REQUIREMENTS.md`
- `.planning/milestones/1.0-MILESTONE-AUDIT.md`

**What's next:** Start fresh requirements and roadmap with `$gsd-new-milestone`.

---
