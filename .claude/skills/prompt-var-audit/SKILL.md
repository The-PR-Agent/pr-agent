---
name: prompt-var-audit
description: Audit a prompt TOML under pr_agent/settings/ against the vars dict of the tool that renders it, so a StrictUndefined failure is caught before runtime. Also checks the file is registered in config_loader.py.
disable-model-invocation: true
---

# Prompt variable audit

Prompt rendering uses Jinja2 with `StrictUndefined`. A variable referenced by a template but
missing from the tool's `self.vars` raises at render time — not at import, not in most unit
tests, and typically only on the first real PR that takes that branch. This skill cross-checks
the two sides by hand.

Run it whenever a prompt TOML or a tool's `self.vars` changes.

## Inputs

One prompt file (for example `pr_agent/settings/pr_reviewer_prompts.toml`), or the tool that
owns it. Tool and prompt names correspond:

- `pr_reviewer.py` ↔ `pr_reviewer_prompts.toml`
- `pr_description.py` ↔ `pr_description_prompts.toml`
- `pr_code_suggestions.py` ↔ `code_suggestions/pr_code_suggestions_prompts.toml` and variants

## Steps

1. **Registration.** Confirm the prompt file's `settings/…` path appears in the
   `settings_files=[...]` list in `pr_agent/config_loader.py`. If it does not, the file is never
   loaded into `global_settings` — stop and report that first.

2. **Extract template references.** From the prompt TOML, collect every Jinja reference and
   every condition:

   ```bash
   grep -oE '\{\{[^}]*\}\}|\{%[^%]*%\}' <prompt.toml> | sort -u
   ```

   Reduce to root variable names — for `{{ pr_files[0].filename }}` the root is `pr_files`;
   inside a `{% for f in pr_files %}` the loop variable `f` is local, not a required var.

3. **Extract the vars dict.** In the owning tool, read the `self.vars = {` block and list its
   keys. Include anything merged in later (`self.vars.update(...)`, per-branch assignments).

4. **Diff the two sets.**
   - **In template, not in vars** → a `StrictUndefined` crash on any path that renders it.
     This is the finding that matters.
   - **In vars, not in template** → dead variable, or a sign the template was edited without
     the tool. Report, do not remove without checking the other prompt variants.
   - A variable used only inside `{% if %}` still must exist — `StrictUndefined` raises on the
     condition itself. Optional values must be defined explicitly (commonly as `""`, `[]`, or
     `False`), with the section guarded by the conditional.

5. **Check the variants.** Several tools render more than one prompt file from one vars dict
   (description has three, code suggestions has three). Run step 2 against every variant the
   tool can select before concluding.

6. **Prove it.** For any variable added or made conditional, name the unit test under
   `tests/unittest/` that renders the template, or state that no test covers this path.

## Output

- Registration: ok / missing.
- Missing vars: `variable` → template file:line → the crash it produces.
- Unused vars: list, with a note on whether a sibling variant uses them.
- Verdict: safe to land, or the exact edits required.

Report only. Do not edit prompts or tools as part of the audit — a fix should be a separate,
deliberate change.
