# Architecture Research — Describe Enhancement Integration

**Domain:** Brownfield enhancement to `describe` command (PR-Agent tool)
**Researched:** 2026-07-02
**Confidence:** HIGH (grounded in direct source read of the target files)

## Scope

Identify the exact seams inside PR-Agent's `describe` flow where two new behaviors plug in:

1. **Angular-convention title rewriting** — rewrite the AI title to `type(scope): summary`.
2. **Org template prepend** — put the org's fixed What/Risk/Checklist template above the existing walkthrough, with AI-filled What and Risk.

The overall PR-Agent architecture (Command Pattern, GitProvider ABC, litellm handler, Jinja2 prompts) is already in `.planning/codebase/ARCHITECTURE.md`. This file only maps the two new hooks.

## Describe Flow — Method Map

File: `pr_agent/tools/pr_description.py`

```
run()  L95
  ├── _prepare_prediction(model)                     L206   →  self.prediction  (YAML string)
  ├── _prepare_data()                                L448   →  self.data        (parsed dict)
  ├── _prepare_labels()                              L473
  ├── _prepare_pr_answer_with_markers() (marker mode) L500
  │     └── returns (title, body, walkthrough, files)
  └── _prepare_pr_answer()          (default path)   L551
        └── returns (title, body, walkthrough, files)
                                                    L131  pr_body += "\n\n" + walkthrough + "___\n\n"
                                                    L135-154  optional help/config-toggles appended
                                                    L185  title_to_publish = pr_title if generate_ai_title else None
                                                    L186  self.git_provider.publish_description(title_to_publish, pr_body)
```

### Where title originates

- **YAML key:** `title` (declared in `pr_description_prompt.system`, `pr_description_prompts.toml:49`).
- **Parsed in:** `_prepare_data()` — the YAML load populates `self.data` (`pr_description.py:450`); `self.data['title']` is re-ordered (`:459`).
- **Extracted in:** `_prepare_pr_answer()` at `pr_description.py:568` — `ai_title = self.data.pop('title', self.vars["title"])`.
- **Gated by:** `generate_ai_title` (`:569-574`). If `False`, keeps original MR title.
- **Published from:** `run()` at `pr_description.py:185-186`.

**Angular-rewrite hook: `pr_description.py:568-574`** (title selection) or upstream via prompt.

### Where description body is assembled

- **Assembled in:** `_prepare_pr_answer()` (`pr_description.py:551-625`). Iterates `self.data.items()` in order set by `_prepare_data()` re-order block: `User Description`, `title` (popped earlier), `type`, `labels`, `description`, `changes_diagram`, `pr_files`.
- **Walkthrough concatenation:** `run()` line 131: `pr_body += "\n\n" + changes_walkthrough + "___\n\n"` — this appends the file-walkthrough BELOW the descriptive body.
- **Help / config-output appended after** (`:135-154`).
- **Published at:** `pr_description.py:186` in the same call as the title.

**Org-template prepend hook: `run()` between `pr_description.py:128` and `:131`** — after `_prepare_pr_answer()` returns, before the walkthrough is appended, so the final layout becomes `[org template] + [PR-Agent walkthrough] + [help]`.

## GitLab Publish Path

File: `pr_agent/git_providers/gitlab_provider.py`

```python
# pr_description.py:186
self.git_provider.publish_description(title_to_publish, pr_body)

# gitlab_provider.py:486-493
def publish_description(self, pr_title: str, pr_body: str):
    try:
        if pr_title is not None:
            self.mr.title = pr_title      # title set only if not None
        self.mr.description = pr_body     # description always set
        self.mr.save()                     # single round-trip to GitLab
```

Key facts:
- **One method, one API call.** Title and description are updated together via a single `self.mr.save()` — no separate title update endpoint.
- **Null-title guard already exists.** `pr_title is None` skips the title update (used when `generate_ai_title` is off to preserve manual edits per issue #2474).
- **No extra provider methods needed.** Both features can pass through the existing `publish_description(title, body)` contract.

Implication: The Angular rewrite must occur *before* `run()` calls `publish_description`. The rewrite can happen either in the prompt (title comes back conventional) or in Python after `_prepare_pr_answer()`.

## Prompt vs Post-Processing — Recommendation Per Feature

### Feature 1: Angular title rewriting

**Recommendation: Prompt-side (primary), with Python guard-rail.**

Reasoning:
- Angular types (`feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `chore`, `build`, `ci`, `style`) do NOT map 1:1 to PR-Agent's `PRType` enum (`Bug fix`, `Tests`, `Enhancement`, `Documentation`, `Other`). Post-mapping from `type` loses fidelity (e.g. no `perf` / `refactor` / `chore` distinction).
- Scope inference needs semantic reading of the diff (top-level package, module, or feature area). The LLM already has the full diff and filenames; Python only has filenames.
- Prompt-side keeps a single source of truth for title formatting and avoids brittle regex.

**Implementation shape:**
- Add a Jinja2 conditional to `pr_description_prompts.toml` (`system` block, near the `PRDescription` Pydantic schema and the example YAML) that, when a new prompt variable `enable_conventional_title` is truthy, instructs the model:
  - Emit `title` as `type(scope): summary`
  - Constrain `type` to the Angular set
  - Infer `scope` from the diff (kebab-case, single token, omit parens if unknown)
- Add `"enable_conventional_title": get_settings().pr_description.<flag>` to `self.vars` (`pr_description.py:63-78`).
- Python guard-rail in `_prepare_pr_answer()` after `:568`: if flag is on and the returned title doesn't match `^[a-z]+(\([a-z0-9-]+\))?: .+`, log a warning and fall through (do not silently drop). Optional: a minimal regex-normalizer that lowercases the type token.

**Data flow:**
```
enable_conventional_title (config)
        │
        ▼
  self.vars ─────► Jinja2 render ─────► LLM ─────► YAML `title: "fix(gitlab): ..."`
                                                             │
                                                             ▼
                                            _prepare_data → self.data['title']
                                                             │
                                                             ▼
                                            _prepare_pr_answer (L568) → pr_title
                                                             │
                                                             ▼
                                    publish_description(pr_title, pr_body)
```

### Feature 2: Org template prepend

**Recommendation: Hybrid — prompt-side for AI content, Python-side for template assembly.**

Reasoning:
- The template structure (headers, emoji, order, checklist wording) is fixed and must be byte-stable. LLMs drift on formatting.
- Only two fields need AI content: "What does this MR do? Why?" and "Note / Risk". The empty checklist and headings must NOT come from the LLM.
- PR-Agent already emits a `description` (bullet summary) — this can be repurposed for "What/Why", OR a dedicated key can be added. A dedicated key is cleaner because "Note / Risk" is genuinely new and pairing them avoids overloading `description`.

**Implementation shape:**
- Add two new YAML keys to `pr_description_prompts.toml` under a Jinja2 conditional (`enable_org_template`):
  - `what_why: str` — 1-3 bullets, mirrors legacy MR "What/Why".
  - `note_risk: str` — system / performance / security impact; may be `None` or empty string when nothing to report.
- Add `enable_org_template` to `self.vars`.
- Extend `_prepare_data()` re-order block (`pr_description.py:456-471`) to include `what_why` and `note_risk`, so they don't get pushed to the end.
- In `run()`, after `pr_body, changes_walkthrough, pr_file_changes = self._prepare_pr_answer()` (L128) and before the walkthrough concat (L131), branch on the flag:
  - If on: build `org_template_md` from a Python-side template string using `self.data.get('what_why', '')` and `self.data.get('note_risk', '')`, then `pr_body = org_template_md + "\n\n" + pr_body_from_walkthrough` (or replace `pr_body` entirely with `org_template_md` if the PR-Agent description sections are considered redundant with `what_why`).
  - If off: unchanged.

**Design choice to lock in during Phase 1:** Whether to keep PR-Agent's default "type / description" section in the body when the org template is on. Options:
- **(A) Replace** — the org template's What/Why supersedes PR-Agent's `description` section. Cleaner user output.
- **(B) Keep both** — org template on top, PR-Agent section below (still above walkthrough). Duplicated info but preserves upstream behavior.

Requirements (`PROJECT.md` line 34) say "walkthrough is retained below" — silent on the description section. Recommend (A) for clarity; make it a documented decision.

**Data flow:**
```
enable_org_template (config)
        │
        ▼
  self.vars ─────► Jinja2 render ─────► LLM ─────► YAML `what_why`, `note_risk`
                                                             │
                                                             ▼
                                            _prepare_data → self.data
                                                             │
                                                             ▼
              run() branch: assemble ORG_TEMPLATE with data['what_why'] + data['note_risk']
                                        + fixed CHECKLIST markdown
                                                             │
                                                             ▼
              pr_body = ORG_TEMPLATE (+ walkthrough appended at L131 as usual)
                                                             │
                                                             ▼
                                    publish_description(title, pr_body)
```

## Config Gating

File: `pr_agent/settings/configuration.toml` (existing `[pr_description]` section)

Add two new toggles alongside existing keys like `generate_ai_title`, `publish_labels`, `enable_semantic_files_types`:

```toml
[pr_description]
# ... existing keys ...
enable_conventional_title = false     # rewrite MR title as type(scope): summary
enable_org_template       = false     # prepend org What/Risk/Checklist template
```

Reads use the established pattern: `get_settings().pr_description.enable_conventional_title` and `get_settings().pr_description.enable_org_template`. Both must default `false` so upstream behavior is unchanged.

**Branch points:**

| Location | Existing pattern to follow | New branch |
|----------|----------------------------|-----------|
| `pr_description.py` `__init__` `self.vars` (L63-78) | `enable_semantic_files_types`, `enable_custom_labels` | Add `enable_conventional_title`, `enable_org_template` so prompts see them |
| `run()` after L128, before L131 | mirrors L129 `if not is_supported(...)` conditional | `if get_settings().pr_description.enable_org_template: pr_body = build_org_template(self.data) + pr_body` (or full replace) |
| `_prepare_pr_answer()` after L568 (or via prompt only) | mirrors L569-574 `generate_ai_title` branch | Optional guard-rail regex when `enable_conventional_title` is on |
| `pr_description_prompts.toml` system block | mirrors `{%- if enable_semantic_files_types %}` blocks | Two new `{%- if %}` fences around Angular instructions and What/Risk keys |
| `_prepare_data()` re-order block (L456-471) | mirrors `if 'title' in self.data:` lines | Add `what_why`, `note_risk` pops when `enable_org_template` on |

## Component Boundaries (What Changes, What Stays)

| Component | File | Change |
|-----------|------|--------|
| Prompt template | `pr_agent/settings/pr_description_prompts.toml` | Add two Jinja2 conditionals: Angular title instructions, `what_why`/`note_risk` schema + example |
| Config defaults | `pr_agent/settings/configuration.toml` | Add `enable_conventional_title`, `enable_org_template` (default false) |
| Tool class | `pr_agent/tools/pr_description.py` | 3 edits — `self.vars` additions, `_prepare_data()` re-order additions, `run()` body-assembly branch; optional title guard-rail in `_prepare_pr_answer` |
| GitLab provider | `pr_agent/git_providers/gitlab_provider.py` | **No change.** `publish_description(title, body)` already accepts everything needed |
| Other providers | `pr_agent/git_providers/github_provider.py`, etc. | **No change** for v1 (GitLab-only per `PROJECT.md`) |
| AI handler | `pr_agent/algo/ai_handlers/litellm_ai_handler.py` | **No change** |

## Data Flow — End to End (Both Features On)

```
config.toml ── enable_conventional_title=true, enable_org_template=true
     │
     ▼
PRDescription.__init__
     ├── self.vars["enable_conventional_title"] = True
     └── self.vars["enable_org_template"]        = True
     │
     ▼
_prepare_prediction → _get_prediction → Jinja2 render
     ├── Angular block injected into system prompt
     └── what_why / note_risk keys added to schema + example
     │
     ▼
litellm chat_completion → YAML response containing:
     title: "feat(describe): angular titles"
     description: |
       - existing PR-Agent summary
     what_why: |
       - existing legacy What/Why bullets
     note_risk: |
       - risk notes
     pr_files: [...]
     │
     ▼
_prepare_data (load_yaml + re-order incl. new keys)
     │
     ▼
_prepare_pr_answer → returns (pr_title="feat(describe): ...", pr_body=<default sections>)
     │
     ▼
run() body-assembly branch:
     if enable_org_template:
         pr_body = render_org_template(
             what_why = self.data["what_why"],
             note_risk = self.data["note_risk"],
             checklist = FIXED_CHECKLIST_MD,
         )
     │
     ▼
pr_body += walkthrough  (existing L131 concat)
     │
     ▼
publish_description(pr_title, pr_body)  → gitlab_provider L486-493 → self.mr.save()
```

## Build Order

**Phase 1 — Config skeleton (both features, foundation):**
- Add both keys to `configuration.toml` with defaults `false`.
- Add both to `self.vars` in `PRDescription.__init__`.
- Verify no behavior change with defaults off.

**Phase 2 — Angular title (Feature 1, simpler + independent):**
- Extend `pr_description_prompts.toml` `system` block with a Jinja2 conditional.
- Optionally add a Python guard-rail in `_prepare_pr_answer()`.
- Test with real MRs across a few diff types.

**Phase 3 — Org template (Feature 2, depends on new prompt keys):**
- Extend prompt with `what_why` and `note_risk` under `enable_org_template` conditional.
- Update `_prepare_data()` re-order to include new keys.
- Add template renderer + body-assembly branch in `run()`.
- Decide (A) replace vs (B) keep-both for PR-Agent description section — recommend (A).

**Rationale for order:**
- Config first because both features gate on toggles; skeleton lets each feature ship independently.
- Angular title before org template because title work is contained to two edits (prompt + optional guard-rail) with no data-shape changes. Org template requires new YAML keys and body re-assembly.
- No cross-dependencies between features once config skeleton exists — either can be built or reverted independently.

## Anti-Patterns Specific to This Integration

**Do not** post-process the title by mapping PR-Agent's `type` enum to Angular types. The enum lacks `refactor`, `perf`, `chore`, `build`, `ci`, `style`, so accuracy drops. Let the model choose from the Angular set directly.

**Do not** put the fixed template markdown (headers, checklist wording) inside the prompt. LLM drift will break the byte-stable format. Build it in Python from AI-provided field values.

**Do not** overload the existing `description` YAML field with What/Why content. It's easier to reason about ordering, edge cases (empty risk), and the (A)-vs-(B) decision when the two are distinct keys.

**Do not** touch `publish_description` in `gitlab_provider.py`. Its signature already carries everything needed and is shared with other providers; a change there widens blast radius unnecessarily.

**Do not** forget the `_prepare_data()` re-order block (`pr_description.py:456-471`). Any new YAML key added to the prompt should be mentioned here or it silently ends up in an unexpected position in `self.data`.

## Sources

- `pr_agent/tools/pr_description.py` (full read, L1-890) — flow, extraction points, publish path
- `pr_agent/git_providers/gitlab_provider.py` L486-493 — `publish_description` implementation
- `pr_agent/settings/pr_description_prompts.toml` L1-188 — Pydantic schema, Jinja2 conditionals, YAML example
- `.planning/PROJECT.md` — feature scope, org template spec, key decisions
- `.planning/codebase/ARCHITECTURE.md` — existing PR-Agent architecture (grounding)

Confidence: HIGH — all integration points confirmed against current source; no inferred behavior.
