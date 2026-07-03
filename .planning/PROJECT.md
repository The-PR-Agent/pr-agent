# PR-Agent — Org MR Enhancements

## What This Is

A fork of PR-Agent (Qodo Merge), an AI tool that reviews, describes, and improves merge/pull requests. v1.0 enhances the `describe` command for GitLab MRs by rewriting titles to the Angular Commit Convention and prepending the organization's fixed MR description template with AI-filled What/Risk sections while preserving PR-Agent's generated walkthrough.

## Core Value

When a GitLab MR opens, `describe` can produce a conventionally formatted title and org-standard description body with zero manual formatting by the author, while default PR-Agent behavior remains unchanged unless the fork toggles are enabled.

## Current State

- **Shipped:** 1.0 Org MR Enhancements on 2026-07-03.
- **Scope delivered:** GitLab-only `describe` enhancements, config-gated and defaults off.
- **Archive:** `.planning/milestones/1.0-ROADMAP.md`, `.planning/milestones/1.0-REQUIREMENTS.md`, `.planning/milestones/1.0-MILESTONE-AUDIT.md`.
- **Status:** No active milestone. Next milestone starts with `$gsd-new-milestone`.

## Requirements

### Validated

Existing platform behavior relied on by this milestone:

- `describe` command generates and publishes PR/MR title and description via LLM — existing
- `review` command posts AI code review — existing
- `improve` command posts inline code suggestions — existing
- GitLab provider integration publishes title, description, and comments — existing
- CLI entry point (`python -m pr_agent.cli --pr_url=... <command>`) — existing
- Dynaconf hierarchical config with TOML defaults and per-repo overrides — existing
- Jinja2 prompt templating with YAML-parsed LLM responses — existing

v1.0 shipped and audited:

- `enable_conventional_title` toggle exists under `[pr_description]`, defaults false, and is env-overridable — v1.0
- `enable_org_template` toggle exists under `[pr_description]`, defaults false, and is env-overridable — v1.0
- Org template body lives in fork-owned `pr_agent/settings/org_template.md` — v1.0
- With toggles off, `describe` output remains byte-identical to upstream behavior — v1.0
- `enable_conventional_title=true` publishes Angular-style GitLab MR titles without also requiring `generate_ai_title=true` — v1.0
- Angular title validator repairs common defects or falls back to leaving the existing title untouched — v1.0
- `enable_org_template=true` prepends the org template above PR-Agent's default generated body — v1.0
- AI fills What/Why and Note/Risk; checklist remains human-controlled — v1.0
- Re-running `describe` is idempotent and preserves human-ticked checkbox state — v1.0
- Marker mode skips org-template prepend instead of corrupting marker replacement — v1.0
- Non-GitLab providers ignore both org MR feature toggles — v1.0

### Active

None. Define fresh requirements with `$gsd-new-milestone`.

### Out of Scope

- GitHub / Bitbucket / Azure DevOps title and template support — v1 proved the GitLab flow first; cross-provider support remains future work.
- Changes to `review` and `improve` commands — v1 is scoped to `describe`.
- Author-supplied manual template filling — AI fills What/Risk; only checklist state is preserved for humans.
- Dynamic per-MR template structure — template structure remains fixed.
- Angular breaking-change `!` marker and special `revert` form — deferred to v2.
- Per-section hash preservation for manually edited What/Risk content — deferred to v2.

## Context

- Brownfield Python 3.12+ project with FastAPI servers and CLI entry points.
- LLM access flows through litellm. Command Pattern maps commands to Tool classes under `pr_agent/tools/`.
- Primary changed tool: `pr_agent/tools/pr_description.py` (`PRDescription`).
- Fork prompt steering uses runtime `extra_instructions`; shared `pr_agent/settings/pr_description_prompts.toml` stayed untouched.
- Org template idempotency uses HTML comment sentinels and checkbox-state preservation by label.
- Latest audit: `.planning/milestones/1.0-MILESTONE-AUDIT.md` status `passed`.

## Constraints

- **Tech stack:** Python 3.12+, litellm, Jinja2, dynaconf; match existing style and line length.
- **Platform:** v1 behavior is GitLab-only.
- **Compatibility:** All new behavior stays config-gated and defaults off.
- **Architecture:** Stay within existing Tool + prompt-template pattern. Do not add abstractions without immediate need.

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Angular title type/scope inferred by AI from diff | No reliable convention in branch/commit names; AI already reads the diff | Shipped v1.0 with runtime steering and Python validator |
| Invalid Angular title falls back to `None` | GitLab title should remain untouched rather than receive malformed or empty text | Shipped v1.0 |
| AI fills What/Risk; checklist stays manual | Checklist requires human judgment | Shipped v1.0 |
| Keep PR-Agent walkthrough below org template | Retains existing PR-Agent value; org template is additive | Shipped v1.0 |
| GitLab-only for v1 | Matches current MR workflow and avoids premature cross-provider scope | Shipped v1.0; non-GitLab noop tests added |
| Config-gated toggles default off | Fork remains mergeable and safe for upstream/default behavior | Shipped v1.0 |
| Runtime `extra_instructions`, no shared prompt TOML edits | Minimize upstream prompt conflicts | Shipped v1.0 |
| Sentinel-based idempotency | Small v1 mechanism that preserves checkbox state without section hashes | Shipped v1.0; per-section hash deferred |

## Next Milestone Ideas

- Cross-provider support for GitHub, Bitbucket, and Azure DevOps.
- Angular breaking-change marker support.
- Per-section preservation for manually edited What/Risk fields.
- Operational rollout docs for CI toggles and GitLab MR usage.

---
*Last updated: 2026-07-03 after 1.0 milestone completion*
