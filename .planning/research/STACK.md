# Stack Research

**Domain:** Brownfield enhancement to PR-Agent `describe` — Angular-conventional MR titles + templated MR descriptions for GitLab
**Researched:** 2026-07-02
**Confidence:** HIGH (all recommendations grounded in files read at `pr_agent/tools/pr_description.py`, `pr_agent/settings/pr_description_prompts.toml`, `pr_agent/settings/configuration.toml`; Angular spec quoted from authoritative sources)

## Recommended Stack

No new runtime dependencies. Everything needed is already in the fork:

### Core Technologies (reuse — do not add)

| Technology | Version (locked in `requirements.txt`) | Purpose | Why Recommended |
|------------|----------------------------------------|---------|-----------------|
| Jinja2 | 3.1.6 | Prompt templating with `StrictUndefined` (already used in `pr_description.py:432`) | The Angular rules and org template will be injected as additional prompt sections — same rendering path, no new dependency |
| litellm | 1.84.0 | Unified LLM call (used at `pr_description.py:439` via `self.ai_handler.chat_completion`) | Prediction pipeline already returns raw YAML; new fields (`type`, `scope`, filled sections) live inside the existing YAML response |
| PyYAML (via `load_yaml` in `pr_agent/algo/utils.py`) | pinned | Parse LLM response | `_prepare_data()` already loads `title:` + `description:` keys — extend with additional keys |
| dynaconf | 3.2.4 | Config toggles read as `get_settings().pr_description.<key>` | New toggles + default template string slot into the existing `[pr_description]` TOML section |

### Supporting Libraries (already present, no action)

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| loguru | 0.7.2 | Structured logging | Log when a toggle is disabled / when regex validation of the AI title fails and a fallback is used |
| tenacity | 8.2.3 | Retry (via `retry_with_fallback_models`) | Already wraps `_prepare_prediction`; keep as-is |
| pydantic | 2.13.3 | Schema types in prompt docstring | The prompt describes the LLM contract via a Pydantic-like class; extend that class in the Jinja template |

### Development Tools (already configured)

| Tool | Purpose | Notes |
|------|---------|-------|
| Ruff | Lint + import sort, 120-char line limit (`pyproject.toml [tool.ruff]`) | Respect existing style; keep prompt strings as triple-quoted TOML |
| pytest 9.0.2 + pytest-asyncio | Test runner | Add a unit test that feeds a canned diff, mocks `chat_completion` to return a YAML with `type`, `scope`, `title`, `description_what`, `description_risk`, and asserts the assembled body starts with the org template and that the title matches `^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([^)]+\))?: [a-z].{0,70}[^.]$` |
| Bandit | Security lint | Nothing new — no shelling out, no external network calls |

## Installation

```bash
# No new packages. The fork's requirements.txt already pins everything.
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

## Part 1: Angular Commit Convention — Exact Rules the AI Prompt Must Enforce

**Sources (verified):**
- `angular/angular/contributing-docs/commit-message-guidelines.md` (authoritative for the Angular flavor of the convention)
- `conventionalcommits.org v1.0.0` (the base spec Angular extends)

### 1.1 Valid TYPES (exhaustive list the prompt should whitelist)

The Angular contributing guide lists exactly these eight types:

- `build` — changes affecting the build system or external dependencies
- `ci` — changes to CI configuration files and scripts
- `docs` — documentation only changes
- `feat` — a new feature
- `fix` — a bug fix
- `perf` — a code change that improves performance
- `refactor` — a code change that neither fixes a bug nor adds a feature
- `test` — adding missing tests or correcting existing tests

Conventional Commits v1.0.0 additionally allows `chore`, `style`, and `revert` and treats them as "other types not mandated by the spec". The user's question explicitly asked for all eleven, so include the full set in the prompt whitelist for maximum coverage:

`feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`

Rationale: matching upstream Angular exactly would be too strict for a general org (no `chore` means routine deps bumps get labelled `build`, which is awkward). The broader eleven-type list is what most tooling (`commitlint @commitlint/config-conventional`, `semantic-release`, `standard-version`) actually recognises.

### 1.2 Header Format

Angular spec (verbatim from the header diagram):

```
<type>(<scope>): <short summary>
```

- `<type>` — REQUIRED, one of the whitelist above
- `(<scope>)` — OPTIONAL, parenthesized noun describing the affected section of the codebase
- `:` + single space — REQUIRED separator (Conventional Commits: "REQUIRED terminal colon and space")
- `<short summary>` — REQUIRED

Breaking change marker (Conventional Commits): a `!` MAY be placed immediately before the `:` (e.g. `feat(api)!: drop legacy endpoint`) to signal a breaking change. Whether to expose this in the AI prompt is a product call; recommendation is to omit it in v1 to keep the AI's output space tight and let humans add `!` manually when needed. (LOW-risk deferral — easy to add later.)

### 1.3 Subject Line Rules (three hard rules from Angular)

Direct quotes from `angular/contributing-docs/commit-message-guidelines.md`:

1. "use the imperative, present tense: 'change' not 'changed' nor 'changes'"
2. "don't capitalize the first letter"
3. "no dot (.) at the end"

Length rule: **Angular does not specify an exact character limit in the current guide.** The widely-cited 50/72 rule comes from Tim Pope's git commit style, not Angular. Recommendation: instruct the AI to keep the whole header (type + scope + summary) at 50–72 characters, hard-cap the summary alone at 72. This is defensible convention without being a fake quote.

Consolidated regex the code can use to validate the AI's output (post-parse, as a defence-in-depth check after the LLM):

```python
ANGULAR_TITLE_RE = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(\([a-z0-9][a-z0-9\-_/.]*\))?"
    r"!?"
    r": "
    r"[a-z].{0,71}[^.]$"
)
```

If the regex fails, log a warning via `loguru` and fall back to the original `self.vars["title"]` — consistent with the existing "graceful fallback on error" convention in the codebase.

### 1.4 How Scope Is Chosen

Angular ties scope to npm package names: "The scope should be the name of the npm package affected (as perceived by the person reading the changelog generated from commit messages)."

For a general org codebase without npm packages, the AI needs a scope-derivation strategy. Recommended prompt guidance (in priority order):

1. If the diff touches files under a single top-level directory (e.g. `pr_agent/tools/`, `src/api/`), use that directory's leaf name as scope (`tools`, `api`).
2. If a single module/service dominates (>=70% of changed lines), use its name.
3. If the change spans many areas, omit the scope entirely — Angular explicitly permits an empty scope for "changes that are done across all packages".
4. Never invent a scope not visible in the diff paths.

Confidence: HIGH on rules 1.1–1.3 (verbatim spec), MEDIUM on 1.4 (extrapolation from the npm-specific Angular text to generic codebase).

## Part 2: PR-Agent `describe` Pipeline — Confirmed Wire Points

Grounded in the actual file reads. All file paths absolute-from-repo-root.

### 2.1 The Pipeline (verified, no ambiguity)

1. **Tool class:** `pr_agent/tools/pr_description.py` → `class PRDescription`.
2. **Entry point:** `PRDescription.run()` at line 95.
3. **Prompt rendering:** `_get_prediction()` at line 428 loads `pr_description_prompt.system` and `pr_description_prompt.user` from settings, renders them with `Environment(undefined=StrictUndefined)` at line 432, and calls `self.ai_handler.chat_completion(...)` at line 439.
4. **Prompt file:** `pr_agent/settings/pr_description_prompts.toml` — two multi-line TOML strings under `[pr_description_prompt]`: `system` and `user`. The variables available for Jinja include `title`, `branch`, `description`, `language`, `diff`, `extra_instructions`, `commit_messages_str`, `enable_custom_labels`, `custom_labels_class`, `enable_semantic_files_types`, `related_tickets`, `include_file_summary_changes`, `duplicate_prompt_examples`, `enable_pr_diagram` (populated in `PRDescription.__init__` at line 63).
5. **Response parsing:** `_prepare_data()` at line 448 calls `load_yaml(self.prediction.strip(), keys_fix_yaml=self.keys_fix)`. `self.keys_fix` is defined at line 49: `["filename:", "language:", "changes_summary:", "changes_title:", "description:", "title:"]`.
6. **YAML keys the tool currently pops from the parsed dict:** `title`, `type`, `labels`, `description`, `changes_diagram`, `pr_files`, plus `User Description` (see lines 456–471).
7. **Body assembly:** `_prepare_pr_answer()` at line 551 walks the remaining dict and emits `### **{Key}**` sections. The `description` key gets special formatting (line 612: bullet-point spacing).
8. **Publish:** `self.git_provider.publish_description(title_to_publish, pr_body)` at line 186. The `title_to_publish` is `pr_title.strip() if get_settings().pr_description.generate_ai_title else None` (line 185) — passing `None` leaves the manual title untouched, per #2474.

### 2.2 Where to Hook the Two New Features

**Feature 1 — Angular-conventional title:**

- **Where to instruct the AI:** Add a Jinja-conditional block at the top of `pr_description_prompt.system` in `pr_agent/settings/pr_description_prompts.toml`, gated on a new variable `use_angular_title_convention` (see Part 3). The block should:
  - List the eleven valid types with one-line meanings.
  - Give the `<type>(<scope>): <summary>` regex-shaped grammar.
  - State the three subject-line rules verbatim ("imperative, present tense", "no capital first letter", "no period at end").
  - Instruct the AI to emit `type_conventional:` and `scope_conventional:` as top-level YAML keys in the response, and to make the `title:` field the pre-assembled `type(scope): summary` string.
- **Where to validate:** In `_prepare_data()` at line 448, after `load_yaml`, when `use_angular_title_convention` is on, run the `ANGULAR_TITLE_RE` from 1.3 against `self.data["title"]`. On failure, log and reassemble from `type_conventional`, `scope_conventional`, and a slugified `description` first-line; if that still fails, fall back to `self.vars["title"]`.
- **YAML keys to add to `self.keys_fix`:** `type_conventional:`, `scope_conventional:`, `summary_conventional:`. This ensures `load_yaml`'s multi-line-key repair handles them.

**Feature 2 — Prepend org description template:**

- **Where to instruct the AI:** In the same Jinja block area of the prompt (gated on `prepend_org_template`), extend the Pydantic-like class description to add two fields: `description_what: str` ("What does this MR do? Why?") and `description_risk: str` ("System, performance, security impact — leave empty string if none"). Also add these to the "Example output" block.
- **Where to assemble the body:** In `_prepare_pr_answer()` (line 551), before the loop that emits `### **{Key}**` sections, pre-pend the rendered org template. Pull the template from a config key `org_description_template` (multi-line string; see Part 3) and render it with `{{what}}` and `{{risk}}` substitutions. The existing PR-Agent walkthrough continues to flow below unchanged, satisfying "PR-Agent's existing generated walkthrough is retained below the org template".
- **Where to gate:** `if get_settings().pr_description.prepend_org_template:` around the prepend block.
- **Do NOT touch `_prepare_pr_answer_with_markers()`:** That path (line 500) handles the marker-based description workflow (`use_description_markers=true`) and is used when authors write `pr_agent:summary` etc. in their MR description. It is orthogonal to this feature and must stay unchanged. Gate the new feature to only apply when `use_description_markers=false` (which is the default, line 123 of `configuration.toml`).

### 2.3 What NOT to Change (upstream mergeability)

- Do **not** rename or remove any existing YAML keys (`title`, `type`, `description`, `pr_files`, `changes_diagram`, `labels`).
- Do **not** change the signature of `_get_prediction`, `_prepare_prediction`, `_prepare_data`, `_prepare_pr_answer`, or `run`.
- Do **not** modify the `use_description_markers` code path (`_prepare_pr_answer_with_markers`).
- Do **not** touch other tool prompts (`pr_reviewer_prompts.toml`, `pr_code_suggestions_prompts.toml`, etc.).
- Do **not** add new dependencies to `requirements.txt`.
- Do **not** change `configuration.toml` defaults for existing keys (e.g. `generate_ai_title=false` must stay `false`; if the new feature requires `generate_ai_title=true` under the hood, flip it programmatically inside `PRDescription.__init__` only when `use_angular_title_convention=true`, and revert on completion — or, cleaner, honour it as a soft requirement documented in a header comment in the TOML).
- Keep changes to `pr_description_prompts.toml` inside Jinja `{% if ... %}` guards so upstream users with the toggles off see byte-identical prompts.

## Part 3: dynaconf Config Pattern — Idiomatic Additions

### 3.1 Reading Settings (existing pattern, confirmed)

From `pr_description.py` lines 51, 61, 62, 69, 73, 116, 120, 125, 135:

```python
get_settings().pr_description.enable_semantic_files_types  # bool
get_settings().pr_description.get("collapsible_file_list_threshold", 8)  # bool/int with default
get_settings().pr_description.extra_instructions  # str
```

Two access styles coexist: attribute access (`.enable_semantic_files_types`) and `.get(key, default)`. Use `.get(key, default)` for new keys — it is the pattern recently added in #2478 ("guard pr_description config reads against missing keys") and matches the fork's most recent commit.

### 3.2 Defining Defaults (idiomatic pattern to follow)

Add to `pr_agent/settings/configuration.toml` in the existing `[pr_description]` block (starts line 103), following the exact style of the surrounding keys (`snake_case`, inline `#` comment for the toggle purpose, TOML basic string for singles, TOML multi-line basic string `"""..."""` for the template):

```toml
[pr_description] # /describe #
# ... existing keys unchanged ...
use_angular_title_convention = false  # rewrite the MR title as `type(scope): summary` per Angular convention
prepend_org_template = false  # prepend the org's What / Risk / Checklist template above the PR-Agent walkthrough
org_description_template = """\
## What does this MR do? Why?
{{what}}

## Note / Risk
<!-- System, performance, security impact -->
{{risk}}

## Checklist
- [ ] Self-reviewed
- [ ] Tested
- [ ] No backward compatibility break
- [ ] Documentation updated (if needed)
"""
```

Rationale for the specifics:

- **Both toggles default to `false`.** Existing PR-Agent behaviour must be preserved when toggles are off (from `.planning/PROJECT.md` constraints and Key Decision "Config-gated toggles"). This is also what the upstream `pr_code_suggestions.demand_code_suggestions_self_review = false` pattern does (line 159).
- **Multi-line TOML `"""..."""` with `\` line continuation on the opening line.** Matches the existing `pr_custom_prompt.prompt = """\ ... """` at line 165 of `configuration.toml` — byte-for-byte the same idiom.
- **`{{what}}` and `{{risk}}` as substitution markers.** These are rendered by Jinja2 (already the templating engine in `pr_description.py:432`); no new templating layer. In the tool code:
  ```python
  template = get_settings().pr_description.get("org_description_template", "")
  rendered = Environment(undefined=StrictUndefined).from_string(template).render(
      what=self.data.get("description_what", "").strip() or "_TBD_",
      risk=self.data.get("description_risk", "").strip() or "_None identified_",
  )
  ```
- **Emoji-free template.** `PROJECT.md` shows the template with emojis (📌, ⚠️, ✅). Two options: (a) keep them and rely on GitLab GFM rendering; (b) omit them for cleaner diffs. Recommendation: keep them, since they were in the org's original template — put them directly in the TOML string.

### 3.3 Per-Repo Override Path (already supported, no action)

The dynaconf hierarchy already accepts per-repo `.pr_agent.toml` overrides (confirmed in `pr_agent/config_loader.py`, and in the codebase STACK.md "Per-repo settings via `.pr_agent.toml`"). This means the org can override `org_description_template` per repo without any code changes. Call this out in the docstring/help comment above the key.

Confidence: HIGH — pattern lifted directly from three existing multi-line-string keys in the same file (`pr_custom_prompt.prompt`, and the extended-mode block starting line 150).

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| Enforce Angular convention inside the existing `pr_description_prompt` via Jinja conditionals | Introduce a separate `pr_angular_title_prompt` and a second LLM call | Only if the added instructions blow past the model's context window for large PRs — not the case here since the prompt currently fits easily. Two LLM calls would double cost and latency. |
| Regex-validate the AI title as defence-in-depth after `load_yaml` | Trust the LLM output as-is | Trust-only is faster but silently ships malformed titles. Regex validation is 5 lines and preserves the existing "graceful fallback" convention. |
| Store `org_description_template` in `configuration.toml` | Store in a separate `org_template.toml` file | A separate file adds a dynaconf loader entry and a load order concern. One key in one existing block is simpler and matches the `pr_custom_prompt.prompt` precedent. |
| Emit `type_conventional` + `scope_conventional` + `title` all in the LLM YAML | Have the LLM emit only `type` and `scope`, assemble title in Python | Both work; emitting the pre-assembled `title` lets the model make one coherent choice about phrasing, and the Python side gets a natural validation target. |
| Gate with two independent toggles (`use_angular_title_convention`, `prepend_org_template`) | One master toggle `enable_org_describe_style` | Two toggles let the org enable them independently (e.g. keep manual titles but adopt the template). Cost is trivial — one extra bool. |

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| Adding `commitlint` or a JS validator as a dep | Fork is Python-only; introducing Node tooling contradicts the stack | Python regex from 1.3, run in-process |
| Overwriting the user's title unconditionally when `use_angular_title_convention=true` | Bug #2474 (referenced at line 185 of `pr_description.py`) was fixed by NOT passing a title when `generate_ai_title=false` — the org toggle needs the same care | When `use_angular_title_convention=true`, treat that as consent to overwrite (equivalent to `generate_ai_title=true`); when off, leave the title alone |
| Emitting the org template via `_prepare_pr_answer_with_markers` | That path fires only when `use_description_markers=true` and the author has written marker comments in the MR description — a totally different flow | Emit the template in `_prepare_pr_answer` (the default path) |
| Trying to enforce Angular convention post-hoc by parsing the LLM's freeform title | Fragile string manipulation; the model already understands the convention if told once | Instruct the model in the prompt, validate output with a single regex, fall back cleanly |
| Adding help-text for the org template to `HelpMessage.get_describe_usage_guide()` | That help text is inserted by the upstream flow at line 137 of `pr_description.py`; keep upstream text intact for mergeability | Document the toggles in a separate `docs/` note or in a header comment in `configuration.toml` |

## Stack Patterns by Variant

**If the AI outputs an invalid Angular title:**
- Log a warning with the raw title.
- Fall back to `self.vars["title"]` (the original MR title).
- Do NOT retry the LLM call — that adds latency for a rare edge case and the fallback is safe.

**If the AI leaves `description_what` empty:**
- Substitute the literal string `_TBD_` so the human sees an obvious placeholder.
- Better UX than a blank section.

**If `use_description_markers=true` (user is using the marker flow):**
- Skip both new features. The marker flow is opt-in and orthogonal.
- Add an early return / guard around the new code paths.

**If the git provider is not GitLab:**
- v1 is GitLab-only per `PROJECT.md`. The features still work over any provider that supports `publish_description`, so there is no need to hard-gate on the provider class. But: log an info message if the provider is not GitLab, so testers running the same command on a GitHub PR see the reason.

## Version Compatibility

| Package A | Compatible With | Notes |
|-----------|-----------------|-------|
| Jinja2 3.1.6 | dynaconf 3.2.4 | Both stable, no interaction concern. Jinja `StrictUndefined` on the template means an undefined `{{what}}` will raise — that is desired: catch in `_get_prediction`'s outer try, fall back to raw template minus substitutions. |
| PyYAML (via `load_yaml`) | `keys_fix_yaml` list | New keys must be added to `self.keys_fix` for the parser's key-repair pass to recognise them across multi-line values. Miss this and YAML with embedded `type_conventional:` fragments in block scalars can misparse. |
| litellm 1.84.0 | any prompt length change | The prompt grows by ~1KB with the Angular rules block; well within any modern LLM context. `TokenHandler` already counts prompt tokens (line 83) and will surface any issue via existing large-PR handling. |

## Sources

- `pr_agent/tools/pr_description.py` (lines 33–204 read directly) — HIGH confidence on pipeline claims
- `pr_agent/settings/pr_description_prompts.toml` (full read) — HIGH confidence on template variables and structure
- `pr_agent/settings/configuration.toml` (lines 100–260 read directly) — HIGH confidence on config idiom
- `.planning/codebase/STACK.md` (already-mapped codebase stack, used as grounding) — HIGH confidence
- `.planning/PROJECT.md` — Requirements, constraints, org template shape
- `angular/angular/contributing-docs/commit-message-guidelines.md` (fetched 2026-07-02) — HIGH confidence, direct quotes for TYPES, header format, three subject rules, scope
- `https://www.conventionalcommits.org/en/v1.0.0/` (fetched 2026-07-02) — HIGH confidence for the base spec, breaking-change marker `!`, the OPTIONAL-scope rule
- Recent commit `254ab008 fix(describe): guard pr_description config reads against missing keys (#2238) (#2478)` on this branch — informs the `.get(key, default)` reading pattern

---
*Stack research for: PR-Agent `describe` Angular title + org template enhancement*
*Researched: 2026-07-02*
