# Project Research Summary

**Project:** PR-Agent — Org MR Enhancements (Angular-convention MR titles + org description template prepend, GitLab-only, config-gated)
**Domain:** Brownfield enhancement to an AI PR-description tool (fork of PR-Agent / Qodo Merge)
**Researched:** 2026-07-02
**Confidence:** HIGH

## Executive Summary

This milestone adds two config-gated behaviors to PR-Agent's `describe` command for GitLab MRs: (1) rewrite the MR title to follow the Angular Commit Convention (`type(scope): summary`), and (2) prepend the organization's fixed description template (What/Why, Note/Risk, Checklist) with the What/Why and Note/Risk sections filled by the AI, while keeping PR-Agent's existing generated walkthrough below. Both must default OFF so upstream behavior is byte-identical when toggles are unset.

Research (grounded in a direct read of `pr_description.py`, `gitlab_provider.py`, `pr_description_prompts.toml`, and `configuration.toml`) confirms **no new dependencies** are required — Jinja2, litellm, dynaconf, and PyYAML already cover everything. The clean approach is: **prompt-side generation for AI content** (Angular title + `what_why`/`note_risk` YAML keys) combined with **Python-side assembly** for anything that must be byte-stable (the fixed template headers, the empty checklist, and a title validation guard-rail). The GitLab publish path (`publish_description(title, body)`) needs zero changes — it already updates title and description in one `mr.save()` call and already null-guards the title.

The dominant risks are all in the org-template feature, not the title feature: **YAML-contract fragility** (AI-filled prose with colons/markdown breaks `load_yaml`), **idempotency** (re-running `describe` must not stack duplicate templates or reset human-ticked checkboxes), and **upstream mergeability** (editing the heavily-mutated shared prompt file creates permanent rebase conflicts). Mitigations: keep new YAML values as `|` block scalars and extend `keys_fix`; use HTML-comment sentinels + `get_pr_description(full=True)` for idempotency; and prefer fork-owned config/template files + gated code paths over inline edits to shared prompts. The title feature needs a Python regex validator as a safety net against malformed AI output, falling back to `None` (leave title untouched) on failure.

## Key Findings

### Recommended Stack

No new libraries. Everything lands in three existing files (plus optionally one new fork-owned template file), staying within PR-Agent's Tool + Jinja2-prompt + dynaconf pattern.

**Core technologies (already present):**
- Jinja2 3.1.6 (StrictUndefined): prompt templating — add `{% if %}`-guarded blocks for the new instructions/keys
- litellm 1.84.0: single LLM call already carries both features' output (no extra round-trip)
- dynaconf 3.2.4: config toggles via `get_settings().pr_description.get(key, default)` (matches the #2478 guard pattern)
- PyYAML (`load_yaml` + `keys_fix`): parses the LLM response — the contract to protect

**Files to touch:**
- `pr_agent/tools/pr_description.py` — `self.vars` additions, `_prepare_data` re-order, `run()` body-assembly branch, optional title guard-rail helper
- `pr_agent/settings/pr_description_prompts.toml` — Jinja conditionals (see mergeability caveat: prefer `extra_instructions`/fork-owned prompt over inline edits)
- `pr_agent/settings/configuration.toml` — new `[pr_description]` toggles + template default (or a separate `org_template.md` file)

### Angular Commit Convention (rules the AI prompt must enforce)

- **Header format:** `<type>(<scope>): <short summary>` — scope optional.
- **Valid types:** `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert` (Angular's 8 + Conventional Commits extras for org realism).
- **Subject rules (verbatim from Angular guidelines):** imperative present tense ("add" not "added"/"adds"), no capitalized first letter, no trailing period. Keep ~50–72 chars.
- **Scope:** single kebab-case token inferred from the diff (top-level package/module/feature area); omit the parens entirely if unknown — never emit empty `()`.
- **Breaking change `!` marker:** OPEN GATE — Stack research defers to v2 (keep AI output tight); Features research calls it table stakes. Decide during roadmap/planning.

### Expected Features

**Must have (table stakes):**
- Angular title: correct `type(scope): summary` across feat/fix/refactor/docs/chore; pick a single primary type for multi-type MRs (no compound `feat/fix`); Python validator + graceful fallback to `None` on malformed output
- Org template: template appears exactly once; **idempotent** re-runs (no duplication); human-ticked checkboxes preserved verbatim; PR-Agent walkthrough retained below; AI fills What/Why + Note/Risk only (never the checklist)
- Both features gated behind toggles defaulting to `false`

**Should have (competitive):**
- Title normalization/repair (lowercase type, strip trailing period, truncate over-length, infer type from PR-Agent's `type` labels) before falling back
- "Human edited the title mid-flow" detection to avoid clobbering manual edits

**Defer (v2+):**
- Hash-based per-section human-edit detection for What/Risk (ship sentinel idempotency first)
- Cross-provider support (GitHub/Bitbucket/Azure) — GitLab only for v1
- Angular `revert` special form (low frequency)

**Anti-features (do NOT build):** AI ticking the checklist; dynamic per-MR template structure; compound Angular types; rewriting titles that already conform (leave or normalize only).

### Architecture Approach

Two seams inside `pr_agent/tools/pr_description.py`, both feeding the unchanged GitLab `publish_description(title, body)` path.

**Major components / hooks:**
1. **Config skeleton** — add toggles to `configuration.toml` + `self.vars` (`__init__`, ~L63–78). No behavior change; establishes the seam both features plug into.
2. **Angular title** — prompt-side generation (`title` YAML key) + Python guard-rail in `_prepare_pr_answer` (~L568) after `ai_title = self.data.pop('title', ...)`. Small blast radius, independently testable.
3. **Org template** — two new YAML keys (`what_why`, `note_risk`) from the LLM; fixed headers + empty checklist assembled in Python in `run()` between L128 and L131 (before the walkthrough concat). Requires `_prepare_data` re-order additions and idempotency handling. Larger blast radius.

**Recommended prompt-vs-post-process:** Angular title = prompt-side + Python guard-rail; org template = hybrid (prompt for AI content, Python for template assembly and byte-stable structure).

### Critical Pitfalls

1. **YAML-contract fragility** — new AI-filled fields with colons/markdown/emojis break `load_yaml`. Avoid: keep values as `|` block scalars, extend `self.keys_fix` with the new key prefixes, update the prompt example block, and never put `##`/emojis/checkboxes inside a YAML value. Assemble the visible headers/checklist in Python, not in the model output.
2. **Idempotency / duplication** — re-running `describe` stacks duplicate templates and resets human checkbox ticks. Avoid: wrap the org block in HTML-comment sentinels (`<!-- pr_agent:org_template:start/end -->`), read the current body via `get_pr_description(full=True)`, replace only the AI sections, and copy the checklist forward verbatim. Idempotency must ship WITH the feature, not later.
3. **Malformed Angular title published anyway** — invalid type, trailing period, empty `()`, over-length. Avoid: `_normalize_angular_title()` helper with regex validation + structural repair + fallback to `None` (reuse the existing #2474 null-title escape hatch). Ship the validator with the feature.
4. **Upstream mergeability** — editing the heavily-mutated `pr_description_prompts.toml` creates permanent rebase conflicts. Avoid: prefer `extra_instructions` / a fork-owned prompt section / a separate `org_template.md`; guard all fork code with `if get_settings()...get(flag, False)`; add new helper methods rather than mutating `_prepare_pr_answer`.
5. **Org template collides with the file walkthrough** — wrong insertion point duplicates or breaks the collapsible `<details>` block; the literal strings `"File Walkthrough"`/`"Diagram Walkthrough"` are split sentinels used by `process_description`. Avoid: fix a documented assembly order, never let the template contain those sentinel phrases (the emoji headers are safe), and add an end-to-end fixture test.
6. **GitLab title API quirk** — `pr_title.strip()` on a whitespace AI response yields `""`, bypassing the `is not None` guard and setting an empty title; 1MB description limit. Avoid: coerce empty/whitespace titles to `None`; truncate the walkthrough (not the template) if over ~900KB; test emoji rendering on the target self-hosted GitLab.
7. **Defaults shipped ON** — surprises existing fork users. Avoid: defaults `false`, store the template in a separate `.md` file (no TOML escaping traps), validate template presence at runtime and disable the feature for the run if missing.

## Implications for Roadmap

Research converges on a config-first, then feature-by-feature sequence. The features are independent once the config seam exists; the title feature is lower-risk and should ship first for early validation, the org template ships second because idempotency raises its complexity.

### Phase 1: Config skeleton & fork-safe seam
**Rationale:** Establishes the toggles, `self.vars` wiring, and the "no shared-prompt edits / defaults-false / template-in-separate-file" conventions before any feature code lands (addresses Pitfalls 4 & 7 upfront).
**Delivers:** `enable_conventional_title` + `enable_org_template` toggles (default false), config reads via the `.get(key, default)` guard, template stored as a fork-owned file/constant, no behavior change.
**Avoids:** Upstream merge conflicts (P4), defaults-on surprise (P7).

### Phase 2: Angular-convention title rewriting
**Rationale:** Lower complexity, contained blast radius, immediately valuable and independently testable; validates AI scope-inference quality against real MRs before tackling the template.
**Delivers:** Prompt-side conventional `title` + `_normalize_angular_title()` guard-rail (regex validate → repair → fallback to `None`); empty/whitespace-title coercion to `None`.
**Uses:** Jinja `{% if %}` prompt block, existing `publish_description` null-title path.
**Implements:** Title-selection hook + validator; adversarial unit tests.
**Avoids:** Malformed title published (P3), GitLab empty-title quirk (P6).

### Phase 3: Org template prepend with idempotency
**Rationale:** Highest complexity (YAML contract + idempotency + assembly order). Ships last so it builds on the proven config seam and title work.
**Delivers:** `what_why` + `note_risk` YAML keys (block scalars, `keys_fix` extended); Python-assembled template with sentinels; idempotent re-runs preserving checklist ticks; walkthrough retained below; marker-mode (`use_description_markers`) incompatibility handled with a WARN + skip.
**Implements:** `run()` assembly branch, `_prepare_data` re-order, idempotency helper reusing `process_description` split logic.
**Avoids:** YAML breakage (P1), duplication/checkbox reset (P2), walkthrough collision (P5).

### Phase Ordering Rationale
- Config seam first so both features plug into a stable, fork-safe foundation.
- Title before template: lower risk, faster validation of AI output quality, no dependency on template work.
- Idempotency is bundled into the template phase (non-negotiable), not deferred.
- Note: Stack research observes both features touch the same three files and *could* ship in one PR; the phase split is about risk isolation and incremental validation, not file boundaries — the roadmapper may collapse Phases 2–3 if the chosen granularity favors fewer phases.

### Research Flags
Phases likely needing deeper research/design during planning:
- **Phase 3:** idempotency parsing + YAML-contract safety are the highest-risk area — needs golden-fixture `load_yaml` tests and an end-to-end MR fixture test.
- **Design gates to resolve before/during planning:** (a) replace vs keep PR-Agent's default `description` section when the org template is on (Architecture recommends **replace**); (b) breaking-change `!` marker in v1 or v2 (conflicting research opinions); (c) whether `enable_conventional_title=true` should programmatically force `generate_ai_title=true` so the new title actually publishes; (d) empty `note_risk` handling (omit / "None" / empty header).

Phases with standard patterns (lighter research):
- **Phase 1 & 2:** well-understood config + prompt patterns already established in the codebase.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | No new deps; wire points grounded in exact file/line reads; dynaconf idiom lifted from existing keys |
| Features | HIGH | Behaviors grounded in PROJECT.md decisions + source; edge cases enumerated systematically |
| Architecture | HIGH | Integration points verified by full read of `pr_description.py`, `gitlab_provider.py`, prompts |
| Pitfalls | HIGH | Each pitfall references concrete code mechanisms (`load_yaml`, `keys_fix`, `PRDescriptionHeader`, `publish_description`) |

**Overall confidence:** HIGH

### Gaps to Address
- **AI scope-inference quality:** real-world Angular `scope` token quality is untested; evaluate after Phase 2 ships against actual MRs and tune the prompt if needed.
- **Token headroom:** adding two output keys on very large diffs — measure on a representative MR early (likely fine given `large_pr_handling`).
- **Self-hosted GitLab emoji/markdown rendering & description size limit:** verify on the target instance, not GitLab.com.
- **Design gates (a)–(d) above:** resolve during roadmap/planning, not left to implementation.

## Sources

### Primary (HIGH confidence)
- `angular/angular` contributing docs — commit message guidelines (types, header format, subject rules), fetched 2026-07-02
- conventionalcommits.org v1.0.0 — extended type set and `!` breaking-change marker, fetched 2026-07-02
- Direct source reads: `pr_agent/tools/pr_description.py`, `pr_agent/git_providers/gitlab_provider.py`, `pr_agent/settings/pr_description_prompts.toml`, `pr_agent/settings/configuration.toml`, `pr_agent/algo/utils.py`
- `.planning/codebase/` map (ARCHITECTURE.md, STACK.md, CONVENTIONS.md, CONCERNS.md)

### Secondary (MEDIUM confidence)
- Angular's npm-specific scope rule extrapolated to a generic codebase (4-rule scope-priority heuristic)

---
*Research completed: 2026-07-02*
*Ready for roadmap: yes*
