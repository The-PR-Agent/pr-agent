# PR-Agent — Org MR Enhancements

## What This Is

A fork of PR-Agent (Qodo Merge) — an AI tool that auto-reviews, describes, and improves merge/pull requests. This milestone enhances the `describe` command for GitLab MRs: rewriting MR titles to follow the Angular Commit Convention and prepending the organization's legacy MR description template with AI-filled sections, while preserving PR-Agent's existing generated walkthrough.

## Core Value

When a GitLab MR opens, the `describe` command produces a conventionally-formatted title and an org-standard description body (What/Risk filled by AI, checklist for the human) on top of the existing PR-Agent walkthrough — with zero manual formatting by the author.

## Requirements

### Validated

<!-- Inferred from existing codebase (see .planning/codebase/). These already ship and are relied upon. -->

- ✓ `describe` command generates and publishes PR/MR title and description via LLM — existing
- ✓ `review` command posts AI code review — existing
- ✓ `improve` command posts inline code suggestions — existing
- ✓ GitLab provider integration (publish title/description/comments) — existing
- ✓ CLI entry point (`python -m pr_agent.cli --pr_url=... <command>`) — existing
- ✓ Dynaconf hierarchical config with TOML defaults + per-repo overrides — existing
- ✓ Jinja2 prompt templating with YAML-parsed LLM responses — existing
- ✓ `describe` rewrites GitLab MR titles to Angular Commit Convention when `enable_conventional_title=true`, with safe `None` fallback — Phase 2

### Active

<!-- New scope for this milestone. Hypotheses until shipped and validated. -->

- [ ] `describe` prepends the org's legacy description template (What does this MR do / Note-Risk / Checklist) at the start of the description body
- [ ] AI fills the "What does this MR do? Why?" and "Note / Risk" sections of the org template
- [ ] The org template checklist is preserved as empty checkboxes for the human author
- [ ] PR-Agent's existing generated walkthrough/file-summary is retained below the org template
- [ ] Both behaviors are gated behind configuration toggles in `configuration.toml`

### Out of Scope

- GitHub / Bitbucket / Azure DevOps title & template support — v1 targets GitLab MRs only; other providers can follow once the GitLab flow is proven
- Changes to `review` and `improve` commands — this milestone is scoped to `describe`
- Author-supplied manual template filling — the AI fills What/Risk; only the checklist stays manual
- Making the org template itself AI-authored per-MR (dynamic template) — the template structure is fixed

## Context

- Brownfield project — existing codebase mapped in `.planning/codebase/` (ARCHITECTURE.md, STACK.md, CONVENTIONS.md, etc.)
- Python 3.12+, FastAPI servers + CLI. LLM access via litellm. Command Pattern: each command maps to a Tool class (`pr_agent/tools/`).
- Target tool: `pr_agent/tools/pr_description.py` (`PRDescription`). Title/description are produced from a Jinja2 prompt (`pr_agent/settings/pr_description_prompts.toml`) and parsed as YAML.
- Current workflow runs three CLI commands on MR open: `describe`, `review`, `improve`.
- Org legacy description template (fixed structure):
  ```
  ## 📌 What does this MR do? Why?

  ## ⚠️ Note / Risk
  <!-- System, performance, security impact -->

  ## ✅ Checklist
  - [ ] Self-reviewed
  - [ ] Tested
  - [ ] No backward compatibility break
  - [ ] Documentation updated (if needed)
  ```
- Existing config pattern: settings read as `get_settings().pr_description.<key>`, defaults in `pr_agent/settings/configuration.toml`.

## Constraints

- **Tech stack**: Python 3.12+, litellm, Jinja2, dynaconf — must match existing patterns (snake_case, Ruff 120-char, loguru logging, graceful fallback on error)
- **Platform**: GitLab MRs only for v1
- **Compatibility**: New behavior must be config-gated so default PR-Agent behavior is unchanged when toggles are off
- **Architecture**: Stay within the existing Tool + prompt-template pattern; avoid introducing new abstractions unless the diff requires it

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Angular title type/scope inferred by AI from diff | No reliable convention in branch/commit names; AI already reads the diff | ✓ Phase 2 — AI is steered with `extra_instructions`; Python validator repairs or falls back to `None` |
| AI fills What/Risk; checklist stays manual | Checklist items require human judgment (self-reviewed, tested) | — Pending |
| Keep PR-Agent walkthrough below org template | Retains existing value; org template is additive context at the top | — Pending |
| GitLab-only for v1 | Matches current MR workflow; avoids multi-provider complexity | — Pending |
| Config-gated toggles | Fork should be able to turn features on/off and stay mergeable with upstream | ✓ Phase 1 — `enable_conventional_title` / `enable_org_template` ship default-off; `describe` byte-identical when off |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-07-03 — Phase 2 complete (Angular-convention title rewriting)*
