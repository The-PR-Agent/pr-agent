# Phase 2: Angular-convention title rewriting - Research

**Researched:** 2026-07-03
**Domain:** Prompt steering + post-processing validator on PR-Agent GitLab publish seam
**Confidence:** HIGH

## Summary

Phase 2 is tightly bounded: hook a pure Python validator (`_normalize_angular_title`) into the existing publish gate in `pr_agent/tools/pr_description.py`, add a config toggle (`enable_conventional_title`) that auto-forces the AI-title publish path without requiring `generate_ai_title=true`, and steer the AI via the existing `extra_instructions` mechanism so `pr_description_prompts.toml` is never edited inline. All four locked design decisions from CONTEXT.md are supported by the actual code shape.

The GitLab provider's `publish_description` unconditionally calls `mr.save()` after mutating `mr.title` and `mr.description`, so passing `None` as the title requires either a guard in `pr_description.py` (existing today at line 210-213) OR a guard inside the provider. The existing code takes the first route: it computes `title_to_publish` conditionally and always calls `publish_description`. Confirmation of that shape is captured below in Landmines.

**Primary recommendation:** Implement CFG-04 as a single expression change at `pr_description.py:212` (widen the ternary to include the new toggle), add `_normalize_angular_title` as a module-level pure helper adjacent to existing helpers in `pr_description.py`, and append an Angular instruction block onto `pr_description.extra_instructions` in the tool's `__init__` or `run()` prelude when the toggle is on.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| CFG-04 | `enable_conventional_title` auto-forces AI-title publish path | Widen ternary at `pr_description.py:212` — see Section 1 |
| TITLE-01 | AI steered to Angular format via extra_instructions | Jinja injection point confirmed — see Section 2 |
| TITLE-02 | Validator enforces Angular type set with synonym repair | Pure helper design — see Section 3 |
| TITLE-03 | Repair rules: lowercase first char, strip trailing `.` only | See Section 3 repair table |
| TITLE-04 | Never invent scope; drop empty parens | Regex + repair rules — see Section 3 |
| TITLE-05 | Over-length summary truncated at word boundary ≤70 | See Section 3 repair table |
| TITLE-06 | Unsalvageable output → `None` → GitLab title untouched | Confirmed at `pr_description.py:210-213` — see Section 4 |
</phase_requirements>

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- Out-of-set commit type: Map common synonyms first (`feature`→`feat`, `bug`/`bugfix`→`fix`, `doc`→`docs`, `misc`→`chore`), then fall back to `None` if type is still outside the Angular set.
- Over-length summary (>70 chars): Truncate at the last whole word that fits within 70 chars, no ellipsis.
- Trailing punctuation: Strip a single trailing `.` only. Leave `?` / `!` untouched.
- Leading capital in summary: Lowercase only the first character, preserving embedded acronyms.
- AI steering: When `enable_conventional_title` is on, append an Angular-format instruction block to the model via `extra_instructions`. No inline edits to `pr_description_prompts.toml`.
- Validator hook location: Call `_normalize_angular_title` on `pr_title` at the publish seam (~`pr_description.py:212`), just before `publish_description`. Leave `_prepare_pr_answer` / `_prepare_pr_answer_with_markers` untouched.
- Scope inference during repair: Validator never invents scope. If AI omits scope or emits empty `()`, parens are dropped entirely.
- Fallback wiring: When `_normalize_angular_title` returns `None`, pass `None` to `publish_description` — existing provider path already leaves the title untouched.

### Claude's Discretion
- Exact regex composition of `_normalize_angular_title` internals (locked target regex must be satisfied on output).
- Exact placement of the extra_instructions augmentation (tool `__init__` vs. `run()` prelude).

### Deferred Ideas (OUT OF SCOPE)
- Breaking-change `!` marker (`type(scope)!: summary`) — deferred to v2.
- Scope inference from changed file paths — explicitly rejected for v1.
</user_constraints>

## 1. Publish Seam & CFG-04 Auto-Force

### Exact publish gate lines

From `pr_agent/tools/pr_description.py:209-213`:

```python
else:
    # Pass None when the title is not AI-generated so the provider
    # leaves it untouched, avoiding reverting a manual edit (#2474).
    title_to_publish = pr_title.strip() if get_settings().pr_description.generate_ai_title else None
    self.git_provider.publish_description(title_to_publish, pr_body)
```

The ternary on line 212 is the CFG-04 seam. `pr_title` at this point is whatever `_prepare_pr_answer()` (line 155) or `_prepare_pr_answer_with_markers()` (line 153) selected — which is either `self.vars["title"]` (original) or `self.data.pop('title', ...)` (AI-generated) depending on `generate_ai_title`.

### CFG-04 implementation (auto-force)

Both `generate_ai_title` and the new `enable_conventional_title` need to route through the AI-title branch. The minimal, rebase-safe change is to widen the gate. Two equivalent forms:

Form A — augment the ternary condition (single-line diff, most rebase-friendly):

```python
_pd = get_settings().pr_description
_ai_title_publish = _pd.generate_ai_title or _pd.get('enable_conventional_title', False)
title_to_publish = pr_title.strip() if _ai_title_publish else None
```

However, because `pr_title` upstream (in `_prepare_pr_answer` at line 596 and `_prepare_pr_answer_with_markers` at line 532) still checks *only* `generate_ai_title`, the `pr_title` variable will be `self.vars["title"]` (the original human title) when `generate_ai_title=false` even if `enable_conventional_title=true`. That means Form A alone would just re-publish the human title verbatim after normalization — wrong outcome for CFG-04.

The locked decision says "Leave `_prepare_pr_answer` / `_prepare_pr_answer_with_markers` inline logic untouched (success criterion #4, TITLE-05)." So we must not edit lines 531-537 or 595-601.

**Resolution:** compute the AI title separately at the publish seam, in addition to the current `pr_title`. The AI title is already available on `self.data['title']` before `_prepare_pr_answer*` pops it — but `.pop('title', ...)` has already removed it by the time the publish gate runs. Fix: capture the AI title into `self.ai_title` (or similar) inside the tool before publish, OR re-derive it from `self.vars` if the AI didn't produce one.

Cleanest option honoring "untouched inline logic" is to stash the AI title on `self` inside a small pre-publish helper (still outside `_prepare_pr_answer*`). A concrete shape:

```python
# ~pr_description.py:211 — replacement of lines 210-213
_pd = get_settings().pr_description
_conv_on = _pd.get('enable_conventional_title', False)
if _pd.generate_ai_title:
    title_to_publish = pr_title.strip()
elif _conv_on:
    # CFG-04: auto-force the AI-title path without requiring generate_ai_title
    ai_title_raw = pr_title.strip()  # pr_title already reflects AI or fallback per generate_ai_title
    title_to_publish = ai_title_raw
else:
    title_to_publish = None

if _conv_on and title_to_publish is not None:
    title_to_publish = _normalize_angular_title(title_to_publish)  # may return None → fallback

self.git_provider.publish_description(title_to_publish, pr_body)
```

**Problem with the shape above:** when `generate_ai_title=False` and `enable_conventional_title=True`, `pr_title` at this line is the *original human title* (per line 598 `title = self.vars["title"]`). That's not what CFG-04 says — CFG-04 says the operator shouldn't have to also set `generate_ai_title=true`, implying the AI title should be published when the conventional toggle is on.

The cleanest way without touching lines 531-537 / 595-601: read the AI title from `self.data` *before* `_prepare_pr_answer*` pops it, OR reconstruct via a `getattr`. Since `_prepare_pr_answer*` does `self.data.pop('title', self.vars["title"])`, after either helper runs, `self.data` no longer contains `'title'`. The AI title has been consumed.

**Recommended approach:** stash `ai_title` on `self` at the top of `run()` right after `_get_prediction()`, before `_prepare_pr_answer*` is called (line 152-155). Then the publish seam reads `self.ai_title` regardless of `generate_ai_title`. This keeps `_prepare_pr_answer*` bytes-identical.

```python
# ~pr_description.py:151 (after prediction, before _prepare_pr_answer*)
self.ai_title = (self.data.get('title') or self.vars.get('title') or '').strip()

# ~pr_description.py:212 (publish seam, replacement)
_pd = get_settings().pr_description
_conv_on = _pd.get('enable_conventional_title', False)
if _pd.generate_ai_title:
    title_to_publish = pr_title.strip()
elif _conv_on:
    title_to_publish = self.ai_title
else:
    title_to_publish = None

if _conv_on and title_to_publish:
    title_to_publish = _normalize_angular_title(title_to_publish)  # None → fallback

self.git_provider.publish_description(title_to_publish, pr_body)
```

**Precise insertion point for `_normalize_angular_title` call:** immediately after the `title_to_publish` computation on line 212, guarded by `_conv_on`, and before `publish_description` on line 213. When `_normalize_angular_title` returns `None`, the value flows through to `publish_description` unchanged — the provider then leaves the title untouched (see Section 4).

### Rebase-safety notes

- The current line 212 is a single expression; widening it into a small block is a mechanically simple rebase target.
- Upstream PR #2474 (referenced in the code comment) explicitly established the `None`-preserves-title contract — future upstream changes will preserve that contract or the tests would break.
- No new imports needed if `_normalize_angular_title` lives in the same module.

## 2. extra_instructions Steering

### Injection point in the prompt template

From `pr_agent/settings/pr_description_prompts.toml` lines 11-17 (part of the system prompt):

```jinja
{%- if extra_instructions %}

Extra instructions from the user:
=====
{{extra_instructions}}
=====
{% endif %}
```

The variable is `extra_instructions`. It is a top-level Jinja variable in the template, populated from `self.vars["extra_instructions"]` (standard PR-Agent tool convention — the tool builds a `self.vars` dict and passes it to Jinja). For `pr_description`, this reads from `get_settings().pr_description.extra_instructions` (default `""` per `configuration.toml:108`).

### Augmentation strategy (no prompt-TOML edits)

The Angular-format steering block is appended to the effective `extra_instructions` value at runtime, before the prompt is rendered. Two candidate insertion points inside `pr_description.py`:

1. **In `__init__`, after settings are read.** Mutate the runtime settings once:
   ```python
   if get_settings().pr_description.get('enable_conventional_title', False):
       existing = get_settings().pr_description.get('extra_instructions', '') or ''
       angular_block = (
           "\n\n=== Title format (Angular Commit Convention) ===\n"
           "The `title` field MUST follow: `type(scope): summary`.\n"
           "- `type` MUST be one of: feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert.\n"
           "- `scope` is an optional single kebab-case token; omit the parentheses entirely if unknown.\n"
           "- `summary` MUST be lowercase, imperative mood, no trailing period, and no more than 70 characters.\n"
           "- Do NOT include a leading `#`, quotes, or backticks.\n"
       )
       get_settings().pr_description.extra_instructions = existing + angular_block
   ```

2. **In `run()` or `_prepare_prediction`, before `self.vars` is materialized.** Same effect, but locally scoped — assign to `self.vars["extra_instructions"]` directly after it is built. Requires locating the exact line where `self.vars` is populated (uses `get_settings().pr_description.extra_instructions`).

**Recommendation:** Option 1 (mutate settings in `__init__`) is simpler and matches how Phase 1's fork seams operate (guarded, config-gated, comment-delimited). Downside: it mutates the global Dynaconf singleton for the duration of the request — safe in webhook contexts because `starlette_context` deep-copies settings per request (per `config_loader.py`), and safe in CLI contexts because the process is short-lived.

**Landmine:** avoid double-appending across retries or repeated `run()` calls on the same tool instance. Because `__init__` runs once per tool construction and `PRAgent` creates a fresh tool per command dispatch (see `pr_agent/agent/pr_agent.py`), one-shot append is safe.

## 3. Validator Design

### Signature

```python
def _normalize_angular_title(title: str) -> str | None:
    """Validate/repair `title` against the Angular Commit Convention.

    Returns a repaired, valid Angular title when salvageable; returns None to
    signal the caller should leave the pre-existing MR title untouched.
    """
```

### Target regex (must match on return value)

```
^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([a-z0-9\-]+\))?: [a-z].{1,70}[^.]$
```

Note the summary portion of the regex allows `[a-z]` as first char, then `.{1,70}` (up to 70 additional chars), so total summary length is 2–71 chars. The final char cannot be `.`.

### Constants (module-level)

```python
_ANGULAR_TYPES = frozenset({
    "feat", "fix", "docs", "style", "refactor",
    "perf", "test", "build", "ci", "chore", "revert",
})
_TYPE_SYNONYMS = {
    "feature": "feat",
    "features": "feat",
    "bug": "fix",
    "bugfix": "fix",
    "hotfix": "fix",
    "doc": "docs",
    "documentation": "docs",
    "misc": "chore",
    "chores": "chore",
    "reverts": "revert",
    "tests": "test",
    "testing": "test",
    "refactoring": "refactor",
    "performance": "perf",
    "styles": "style",
    "builds": "build",
}
_MAX_SUMMARY = 70
_ANGULAR_TITLE_RE = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(\([a-z0-9\-]+\))?: [a-z].{1,70}[^.]$"
)
```

### Algorithm (linear, deterministic)

1. Guard: if `title is None` or `not title.strip()`, return `None`.
2. Collapse internal whitespace/newlines: `title = " ".join(title.split())`.
3. Strip a single leading `#` and surrounding backticks/quotes if present (defensive; AI sometimes emits markdown decorations).
4. Split on the FIRST `:` — left side is `header`, right side is `summary`.
   - If no `:` present → return `None`.
5. Parse `header`:
   - Extract optional `(scope)` via `re.match(r"^([a-zA-Z]+)(?:\(([^)]*)\))?\s*$", header)`.
   - If parse fails → return `None`.
   - `type_raw`, `scope_raw` (may be empty or `None`).
6. Normalize type:
   - `type_norm = type_raw.strip().lower()`.
   - If `type_norm in _ANGULAR_TYPES` → keep it.
   - Elif `type_norm in _TYPE_SYNONYMS` → `type_norm = _TYPE_SYNONYMS[type_norm]`.
   - Else → return `None`.
7. Normalize scope:
   - If `scope_raw` is `None` or `scope_raw.strip() == ""` → drop parens (never emit `()`).
   - Else lowercase, strip, replace inner spaces with `-`, and validate against `^[a-z0-9\-]+$`. If it doesn't match after normalization → drop parens (per CONTEXT: never invent scope; failing to salvage scope is not fatal to the whole title).
8. Normalize summary:
   - `summary = summary.strip()`.
   - If empty → return `None`.
   - Strip a single trailing `.` if present (leave `?`/`!` untouched).
   - Lowercase only the first character: `summary = summary[0].lower() + summary[1:]`.
   - If `len(summary) > _MAX_SUMMARY`: truncate at last whole-word boundary that fits:
     ```python
     truncated = summary[:_MAX_SUMMARY]
     if " " in truncated:
         truncated = truncated.rsplit(" ", 1)[0]
     summary = truncated.rstrip().rstrip(".")
     ```
   - If `len(summary) < 2` → return `None` (regex requires `[a-z].{1,70}`, minimum 2 chars total).
9. Reassemble:
   - `header_out = type_norm + (f"({scope_norm})" if scope_norm else "")`
   - `title_out = f"{header_out}: {summary}"`
10. Final validation: `if _ANGULAR_TITLE_RE.match(title_out): return title_out`.
11. Fallback: return `None`.

### Adversarial fixture table

| # | Input | Expected Output | Repair Path |
|---|-------|-----------------|-------------|
| 1 | `Feature(auth): add SSO support` | `feat(auth): add SSO support` | synonym `feature`→`feat`; lowercase first char no-op |
| 2 | `bug: Fix crash on empty input.` | `fix: fix crash on empty input` | synonym `bug`→`fix`; lowercase `F`→`f`; strip trailing `.` |
| 3 | `feat(): initial commit` | `feat: initial commit` | drop empty parens |
| 4 | `feat( ): initial commit` | `feat: initial commit` | whitespace-only scope → drop parens |
| 5 | `feat(User Auth): add SSO` | `feat(user-auth): add SSO` | lowercase + space→hyphen in scope |
| 6 | `feat(User_Auth): add SSO` | `feat: add SSO` | underscore invalid → drop parens (never invent) |
| 7 | `chore: ` | `None` | empty summary |
| 8 | `chore:` | `None` | no summary after colon |
| 9 | `WIP: something` | `None` | `wip` not a type and not in synonyms |
| 10 | `feat add SSO` (no colon) | `None` | header/summary separator missing |
| 11 | `feat(auth): ` + 100 chars | `feat(auth): <first ~70 chars, word-boundary, no trailing period>` | word-boundary truncate |
| 12 | `feat: hello.` | `feat: hello` | strip single trailing `.` |
| 13 | `feat: hello?` | `feat: hello?` | leave `?` untouched |
| 14 | `feat: hello!` | `feat: hello!` | leave `!` untouched |
| 15 | `feat: A` | `None` | after lowercase first char, `a` alone → len < 2 (fails regex minimum) |
| 16 | `feat: AB` | `feat: aB` | lowercase first char only, second-char acronym preserved |
| 17 | `feat: Update API endpoints` | `feat: update API endpoints` | lowercase first char, `API` acronym preserved |
| 18 | `feat:\nadd\nSSO` | `feat: add SSO` | collapse whitespace/newlines |
| 19 | `feat:add SSO` (no space after colon) | `feat: add SSO` | after split on first `:`, `.strip()` on summary normalizes leading space semantics; reassemble with `": "` |
| 20 | `` `feat: add SSO` `` (backtick-wrapped) | `feat: add SSO` | strip surrounding backticks |
| 21 | `#feat: add SSO` | `feat: add SSO` | strip leading `#` |
| 22 | `""` (empty) | `None` | guard |
| 23 | `"   "` (whitespace) | `None` | guard after strip |
| 24 | `Docs: update readme.` | `docs: update readme` | lowercase type; strip trailing `.`; lowercase first char no-op |
| 25 | `Refactoring(api): rename methods` | `refactor(api): rename methods` | synonym `refactoring`→`refactor` |

### Purity guarantees

- No I/O, no logging, no settings access. Pure function on a string.
- Deterministic for a given input.
- Suitable for direct unit testing under `tests/unittest/` per project convention (pytest-asyncio `asyncio_mode = "auto"` is irrelevant — this is a sync helper).

## 4. Landmines

### 4.1 CONFIRMED: `publish_description(None, ...)` leaves title untouched

From `pr_agent/git_providers/gitlab_provider.py:486-493`:

```python
def publish_description(self, pr_title: str, pr_body: str):
    try:
        if pr_title is not None:
            self.mr.title = pr_title
        self.mr.description = pr_body
        self.mr.save()
    except Exception as e:
        get_logger().exception(f"Could not update merge request {self.id_mr} description: {e}")
```

The `if pr_title is not None:` guard is explicit. Passing `None` skips the title mutation entirely, and only the description is updated by `mr.save()`. This is the exact fallback contract TITLE-05/06 relies on — no new provider code is needed, and Phase 1 upstream comment on `pr_description.py:210` already documents this contract (referencing issue #2474).

**Empty string caveat:** an empty string `""` is NOT `None`, so it would overwrite the title with an empty value. `_normalize_angular_title` must return `None` (not `""`) on failure, and the publish seam must not pre-strip in a way that turns `None` into `""`.

### 4.2 Both title-selection paths remain byte-identical

`pr_description.py:531-537` (`_prepare_pr_answer_with_markers`) and `pr_description.py:595-601` (`_prepare_pr_answer`) both contain the identical `ai_title = self.data.pop('title', self.vars["title"])` / `if (not get_settings().pr_description.generate_ai_title): title = self.vars["title"] else: title = ai_title` block. CONTEXT.md success criterion #4 requires both remain untouched.

Consequence for CFG-04: because `pr_title` at line 155/153 reflects `generate_ai_title` only, the publish seam (Section 1) must independently source the AI title (via `self.ai_title` set from `self.data.get('title')` BEFORE `_prepare_pr_answer*` runs) to route the AI-generated title through the validator when `enable_conventional_title=true` but `generate_ai_title=false`.

**Failure mode if this is missed:** with `enable_conventional_title=true, generate_ai_title=false`, the human title would be normalized and re-published (usually as `None` because human titles rarely match Angular already) — surprising behavior. The stash-in-run-prelude pattern in Section 1 avoids this.

### 4.3 Byte-identical when toggle off

When `enable_conventional_title` is off (default), the new code paths must be no-ops:
- `__init__` extra_instructions augmentation: guarded by `if get_settings().pr_description.get('enable_conventional_title', False)` — no mutation when off.
- Publish seam: the widened conditional must degrade to the exact original expression `pr_title.strip() if generate_ai_title else None`. Suggested shape (from Section 1) uses `if _pd.generate_ai_title: ... elif _conv_on: ... else: title_to_publish = None`. When `_conv_on` is false, this collapses to `if generate_ai_title: pr_title.strip() else: None` — semantically identical to the original single-line ternary.
- `_normalize_angular_title` is defined but never called when `_conv_on` is false.
- `self.ai_title` stash: harmless — a new attribute set on the tool instance. No observable effect unless read.

Success criterion: with `enable_conventional_title=false`, byte-for-byte identical GitLab MR title/body output vs. Phase 1.

### 4.4 Test convention: pytest asyncio_mode=auto

Per `pyproject.toml` `[tool.pytest.ini_options]` (project constraints), `asyncio_mode = "auto"`. However, `_normalize_angular_title` is a sync pure function — tests should be `def test_*` (not `async def`) and require no fixtures. Place under `tests/unittest/test_normalize_angular_title.py` (matches project convention of `test_*` prefix per `tests/unittest/test_clip_tokens.py` etc.).

Publish-seam tests (integration-flavor) should mock `git_provider.publish_description` and assert the tuple `(title_to_publish, pr_body)` under each combination of `(generate_ai_title, enable_conventional_title)`. These CAN be sync — they don't exercise the async prediction path, only the publish-decision expression.

### 4.5 Dynaconf mutation in `__init__`

Mutating `get_settings().pr_description.extra_instructions` in `__init__` writes to the request-scoped Dynaconf settings (per `config_loader.py` context-copy pattern). Verified safe in server mode; in CLI mode the mutation persists for the remainder of the process, which is intentional (single-command lifetime). No cleanup needed.

**Alternative if mutation feels wrong:** compute `angular_block` on-the-fly and concatenate into `self.vars["extra_instructions"]` at the point where `self.vars` is built. This keeps global settings untouched but requires locating the exact `self.vars` construction site.

## RESEARCH COMPLETE

**Phase:** 02 — Angular-convention title rewriting
**Confidence:** HIGH (all four investigation areas confirmed via direct code reads)

### Key Findings
- Publish gate at `pr_description.py:212` is a single ternary; CFG-04 needs a widened conditional plus a `self.ai_title` stash set before `_prepare_pr_answer*` runs (line ~151) — this keeps success criterion #4's "untouched inline logic" honored.
- `pr_description_prompts.toml:11-17` already renders `extra_instructions` via Jinja; augmentation in the tool's `__init__` is the clean, no-inline-edit path.
- `_normalize_angular_title` is a pure sync helper with a deterministic 11-step algorithm; a 25-row adversarial fixture table covers the CONTEXT-locked repair rules.
- `gitlab_provider.py:486-493` confirms `publish_description(None, body)` leaves the MR title untouched via an explicit `if pr_title is not None:` guard — no new provider code needed for TITLE-05/06 fallback.
- Byte-identical off-toggle behavior is achievable because every new branch is guarded by `enable_conventional_title`, and the widened publish conditional collapses to the original ternary when the toggle is off.

**Summary:** Phase 2 is a narrow, well-shaped change: one new pure helper (`_normalize_angular_title`), one toggle read (`enable_conventional_title`), one extra_instructions augmentation in `__init__`, and one small refactor of the publish-seam conditional at `pr_description.py:210-213` plus a `self.ai_title` stash near line 151. All four locked design decisions in CONTEXT.md map cleanly onto the existing code shape with no architectural friction.

