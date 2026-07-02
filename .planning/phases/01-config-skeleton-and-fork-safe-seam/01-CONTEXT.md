# Phase 1: Config skeleton and fork-safe seam - Context

**Gathered:** 2026-07-02
**Status:** Ready for planning

<domain>
## Phase Boundary

Establish the two `[pr_description]` toggles (`enable_conventional_title`, `enable_org_template`), both defaulting to `false` and env-overridable through PR-Agent's existing dynaconf `env_loader`. Store the org template body in a fork-owned file (not inlined into the shared upstream prompt TOML), and prove that `describe` output is byte-identical to upstream when both toggles are off. This phase lands the mergeability + defaults-off conventions before any feature code — no title rewriting or template prepending behavior yet (those are Phases 2 and 3).

</domain>

<decisions>
## Implementation Decisions

### Claude's Discretion
All implementation choices are at Claude's discretion — pure infrastructure phase. The ROADMAP goal, success criteria, and locked project decisions fully constrain the work:

- Toggle keys `enable_conventional_title` and `enable_org_template` under `[pr_description]` in `configuration.toml`, both `false`, read via `get_settings().pr_description.get(<flag>, False)`.
- Org template body in a fork-owned file (e.g. `pr_agent/settings/org_template.md`), loaded from Python — never inlined into `pr_description_prompts.toml`.
- Any fork-added code paths in `pr_description.py` guarded by `if get_settings().pr_description.get(<flag>, False):` so upstream rebases only conflict on toggle-reading lines, not `_prepare_pr_answer` internals.
- Env override verified through the existing `config_loader.py:12-18` dynaconf setup (`envvar_prefix=False`, `merge_enabled=True`) — no new plumbing. `PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE=true` / `PR_DESCRIPTION__ENABLE_ORG_TEMPLATE=true` must work.
- Byte-identical when off is the gating success criterion (diff = 0 bytes vs unpatched upstream).

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `pr_agent/config_loader.py` — `get_settings()` returns the Dynaconf singleton (or per-request context copy). `global_settings` built with `envvar_prefix=False`, `merge_enabled=True`, `load_dotenv=False`. Env override already supported via `dynaconf.loaders.env_loader` (config_loader.py:12-18) — this is the mechanism CFG-06 relies on; verify-and-document only.
- `pr_agent/settings/configuration.toml` — `[pr_description]` section starts at line 103. New toggles are appended here.
- `pr_agent/tools/pr_description.py` — `PRDescription` tool; `_prepare_pr_answer` assembles the description. Fork seams must be toggle-guarded here.
- `pr_agent/settings/pr_description_prompts.toml` — shared upstream prompt file; must NOT receive inline org-template content (CFG-03).

### Established Patterns
- Settings read as dotted attributes: `get_settings().pr_description.<key>`; use `.get(key, False)` for absent-safe reads.
- TOML defaults + env override + per-repo `.pr_agent.toml` / `pyproject.toml [tool.pr-agent]` hierarchy.
- Graceful fallback on error, loguru logging, snake_case, Ruff 120-char.

### Integration Points
- `configuration.toml [pr_description]` — new toggle defaults.
- Fork-owned template file under `pr_agent/settings/`.
- `pr_description.py` — guarded seam(s), inert this phase.

</code_context>

<specifics>
## Specific Ideas

No specific requirements — infrastructure phase. Refer to ROADMAP Phase 1 success criteria and the locked decisions in STATE.md (all toggles default false; both env-overridable via existing dynaconf; no inline edits to `pr_description_prompts.toml`).

</specifics>

<deferred>
## Deferred Ideas

None — infrastructure phase.

</deferred>
