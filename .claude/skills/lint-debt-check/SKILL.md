---
name: lint-debt-check
description: Check whether the ruff lint.ignore debt ledger in pyproject.toml has grown against main, and report what it would take to drop a given entry. Run before opening a PR that touches lint config.
disable-model-invocation: true
---

# Lint debt ledger check

`lint.ignore` in `pyproject.toml` is a debt ledger, not configuration. AGENTS.md: entries defer
pre-existing violations so ruff runs green on adoption — "fix the code and drop entries rather
than adding new ones." This skill checks the ledger is shrinking, and sizes the next entry to
burn down.

## Steps

1. **Diff the ledger against the base branch.**

   ```bash
   git show main:pyproject.toml | sed -n '/^lint.ignore = \[/,/^\]/p'
   sed -n '/^lint.ignore = \[/,/^\]/p' pyproject.toml
   ```

   Do the same for `lint.exclude` and `[tool.ruff.lint.per-file-ignores]` — an exclusion added
   there hides the same debt more quietly.

2. **Classify any addition.** A new entry is only acceptable when the rule is genuinely wrong
   for this codebase and the reason is recorded in a comment next to it. A new entry added to
   make a new diff pass is the case to block: report it with the files that would fail without
   it.

   ```bash
   uv run ruff check --select <RULE> --statistics
   ```

3. **Size the next burn-down.** For each ignored rule, count remaining violations and how
   concentrated they are:

   ```bash
   uv run ruff check --select <RULE> --statistics
   uv run ruff check --select <RULE> --output-format concise | awk -F: '{print $1}' | sort | uniq -c | sort -rn | head
   ```

   Rank candidates by low count and low blast radius. Note that `B`-family rules are declared
   `unfixable` in this repo, so those need hand edits, and `E501`/`E722`/`F841` cluster in
   provider and tool files where a mechanical fix can still change behaviour.

4. **Verify a proposed drop.** Remove the entry locally, run `uv run ruff check` on the affected
   paths, and run the tests those files touch with `PYTHONPATH=.`. AGENTS.md: keep lint fixes
   mechanical; if dropping an entry would require a behaviour change, surface that instead of
   applying it.

## Output

- Ledger delta vs `main`: entries added / removed / unchanged.
- Any addition, with the files that motivated it and whether a code fix was available instead.
- Top 3 burn-down candidates: rule, violation count, file concentration, fixable or hand-edit.
- Verdict: one line.
