# Phase 2: Angular-convention title rewriting - Context

**Gathered:** 2026-07-03
**Status:** Ready for planning
**Mode:** Smart discuss (autonomous — batch grey-area proposals, user-accepted)

<domain>
## Phase Boundary

When `enable_conventional_title` is on, `describe` publishes a valid Angular-convention MR
title (`type(scope): summary`) to GitLab. A pure Python validator (`_normalize_angular_title`)
repairs common AI-output defects and safely falls back to leaving the pre-existing GitLab title
untouched (`None`) when the output cannot be salvaged. Enabling the single toggle is sufficient
to publish (auto-forces the AI-title publish path — CFG-04). When the toggle is off, title
behavior is byte-identical to Phase 1 / upstream.

Requirements in scope: CFG-04, TITLE-01, TITLE-02, TITLE-03, TITLE-04, TITLE-05, TITLE-06.

</domain>

<decisions>
## Implementation Decisions

### Validator & Repair Semantics
- **Out-of-set commit type:** Map common synonyms first (`feature`→`feat`, `bug`/`bugfix`→`fix`,
  `doc`→`docs`, `misc`→`chore`), then fall back to `None` if the type is still outside the Angular
  set. Maximizes salvage rate rather than discarding recoverable titles (TITLE-02, TITLE-05).
- **Over-length summary (>70 chars):** Truncate at the last whole word that fits within 70 chars,
  no ellipsis (TITLE-05).
- **Trailing punctuation:** Strip a single trailing `.` only, matching TITLE-03 ("no trailing
  period"). Leave `?` / `!` untouched.
- **Leading capital in summary:** Lowercase only the first character, preserving embedded acronyms
  (e.g. `API`, `URL`) later in the summary (TITLE-03).

### AI Steering & Integration Seam
- **AI steering:** When `enable_conventional_title` is on, append an Angular-format instruction
  block to the model via the existing `extra_instructions` mechanism (config-level). Honors the
  locked project decision "no inline edits to `pr_description_prompts.toml`" (TITLE-01).
- **Validator hook location:** Call `_normalize_angular_title` on `pr_title` at the publish seam
  (~`pr_description.py:212`), just before `publish_description`. Leaves `_prepare_pr_answer` /
  `_prepare_pr_answer_with_markers` inline logic untouched (success criterion #4, TITLE-05).
- **Scope inference during repair:** The validator never invents scope. If the AI omits scope or
  emits empty `()`, the parens are dropped entirely — never emit empty `()` (TITLE-04).
- **Fallback wiring:** When `_normalize_angular_title` returns `None`, pass `None` to
  `publish_description` — the existing provider path (`pr_description.py:210-213`) already leaves
  the pre-existing GitLab title untouched. No new fallback code (TITLE-05, TITLE-06).

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `load_org_template()` / `_ORG_TEMPLATE_PATH` pattern from Phase 1 (`pr_agent/tools/pr_description.py`)
  — the fork-seam idiom: guarded, comment-delimited, config-gated helpers.
- `get_settings().pr_description.get(<flag>, False)` absent-safe accessor established in Phase 1
  and enforced by the guard-audit test.

### Established Patterns
- **Publish gate** (`pr_description.py:210-213`): `title_to_publish = pr_title.strip() if
  get_settings().pr_description.generate_ai_title else None`, then
  `self.git_provider.publish_description(title_to_publish, pr_body)`. Passing `None` leaves the
  existing MR title untouched — this is the ready-made fallback path for TITLE-05/06.
- **Title selection** (`pr_description.py:~531-537` and `~595-601`): `ai_title = self.data.pop('title', ...)`;
  when `generate_ai_title` is false the original `self.vars["title"]` is used. This is the inline
  logic that must remain untouched (criterion #4).
- **Prompt config** (`pr_agent/settings/pr_description_prompts.toml`): fork-owned steering must NOT
  edit this file inline — use `extra_instructions` per the locked decision.

### Integration Points
- CFG-04 auto-force: when `enable_conventional_title` is on, the publish path must behave as if
  AI-title publishing is enabled (do not require the operator to also set `generate_ai_title=true`).
- Validator is a new pure helper `_normalize_angular_title` — module-level, unit-tested in isolation.

</code_context>

<specifics>
## Specific Ideas

- Angular type set (TITLE-02): `feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert`.
- Target validity regex (success criterion #1):
  `^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([a-z0-9\-]+\))?: [a-z].{1,70}[^.]$`.
- Scope is a single kebab-case token when present; parens omitted entirely when unknown (TITLE-04).
- Adversarial fixtures to cover in validator unit tests: empty scope `()`, trailing period,
  capitalized type, invalid type, over-length summary, embedded newlines, whitespace-only,
  empty string — each resolves to a repaired valid title or `None`.

</specifics>

<deferred>
## Deferred Ideas

- Breaking-change `!` marker (`type(scope)!: summary`) — deferred to v2 (locked project decision).
- Scope inference from changed file paths — explicitly rejected for v1; the validator never invents
  scope.

</deferred>
