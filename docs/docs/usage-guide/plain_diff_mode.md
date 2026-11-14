---
title: "Plain-diff local mode"
sidebar_position: 12
---

Run PR-Agent against a raw unified diff with no platform API token and no PR URL.
Results are printed to stdout (and optionally saved to a file). This suits security-first
or air-gapped environments where HTTP access tokens are avoided, and enables pre-push
hooks or CI pipelines that operate on a diff artifact rather than a live pull request.

## Usage

Pipe a diff directly from stdin:

```bash
git diff main...feature-branch | python -m pr_agent.cli --stdin review
```

Or pass a diff file and save the output alongside stdout:

```bash
git diff main...feature-branch > changes.diff
python -m pr_agent.cli --diff-file changes.diff --output review.md review
```

### Flags

| Flag | Description |
|---|---|
| `--stdin` | Read a unified diff from stdin |
| `--diff-file <path>` | Read a unified diff from a file |
| `--output <path>` | Write Markdown produced by a compatible Plain Diff command to a file in addition to stdout |
| `--json-output <path>` | Write structured results and token usage for `review`, `describe`, `ask`, or `generate_labels` (including aliases; `improve` runs `review`; also available with `--pr_url`) |

`--stdin` and `--diff-file` are mutually exclusive. At least one must be provided to
enter plain-diff mode; omitting both falls back to the normal `--pr_url` flow.
Output options must appear before the command. For example, use
`--stdin --output result.md review`, not `--stdin review --output result.md`.
The Markdown and JSON output paths must point to different files.

You can also save structured results from a local description or label run:

```bash
python -m pr_agent.cli --diff-file changes.diff --json-output description.json describe
python -m pr_agent.cli --diff-file changes.diff --json-output labels.json generate_labels
```

These files contain the parsed model result and a `usage` object, while the normal
Markdown output remains available on stdout. The `usage` object is empty when
token usage is unavailable. When all files are excluded, `describe` reports failure instead of claiming a description was generated.
For `generate_labels`, JSON contains only allowlisted model-generated label suggestions,
not existing human-applied PR labels.

### Supported commands

`review`, `describe`, and `ask` are supported, along with their
`review_pr`, `describe_pr`, and `ask_question` aliases. `improve` and `improve_code` run `review`.
`generate_labels` also works with raw diffs and can write its filtered labels as JSON.
Because there is no hosting
platform to push to, `review` writes its findings, including any fixes, as a single markdown
document to stdout (and to `--output`, if given), with each fix as a text block in the
summary, instead of as committable inline suggestions. Commands that require live platform interaction (such as
`update_changelog` or `similar_issue`) are not meaningful in this mode.

By default, `describe` uses `Local diff review` as the title in stdout and `--output`;
set `pr_description.generate_ai_title=true` to use an AI-generated title.

Existing commands that publish Markdown without additional platform state
(`config`, `settings`, and `help`) also honor `--output`.

### Optional PR context

Provide a title and description when the diff needs context that is not apparent
from the code, such as the intended behavior or acceptance criteria:

```bash
python -m pr_agent.cli --diff-file changes.diff review \
  --plain_diff.title="Require approval for invoices" \
  --plain_diff.description="Invoices above the limit need manager approval."
```

`ask` and `describe` also use the supplied title and description. These
optional command arguments need no configuration file changes or enable flag.
Without them, the title remains `Local diff review` and the description remains
empty. Supplying a description may increase model input tokens for commands
that use it, but does not add model requests.

## How it works

1. **Diff parsing** — the unified diff is parsed locally into per-file patch objects.
   Binary files are skipped automatically.

2. **Working-tree enrichment** — when PR-Agent is run inside the repository working tree,
   it reads each changed file from disk and reverse-applies the diff to reconstruct the
   base (pre-change) file content. Having both the base and head versions available gives
   the LLM full file context, which produces higher-quality analysis.

3. **Patch-only fallback** — if a changed file cannot be found on disk (e.g. the diff was
   generated elsewhere, or the file was deleted), PR-Agent falls back to patch-only mode
   for that file. The review still runs; it simply has less context.

4. **Output** — the result is written to stdout. If `--output <path>` is given, it is
   also written to that file (UTF-8, overwritten on each run). Both Markdown outputs
   remove ASCII control characters in the C0 range while preserving tabs and line breaks.
   With `--json-output <path>`, `review`/`review_pr` (and `improve`/`improve_code`, which run it) writes the parsed review, token
   `usage`, and a `coverage` object listing omitted or clipped files.
   `describe`/`describe_pr` writes the parsed description and `usage`.
   `ask`/`ask_question` writes the `question`, `answer`, and `usage`, including a note when no diff is analyzable.
   `generate_labels` writes the filtered `labels` list and `usage`; it never
   combines existing user labels into that list.

No platform token, no PR URL, and no internet access are required for the diff processing
step itself. An LLM API key is still needed unless you configure a local model.

## Difference from the `local` git provider

The existing `git_provider = "local"` mode (invoked with `--pr_url`) computes a diff
by comparing branches in a local Git repository and requires a clean working tree.
The plain-diff mode is different in the following ways:

| | `local` provider | `plain-diff` provider (this page) |
|---|---|---|
| Input | Branch names in a local repo | A unified diff supplied via stdin or file |
| Working tree required | Yes (clean) | No |
| Platform token required | No | No |
| Output | GitHub-style comment published locally | stdout (+ optional file) |
| Inline comments | Not supported | Not supported |

Use the `plain-diff` provider when you already have a diff artifact (e.g. from a CI step or
`git format-patch`) and want a zero-configuration, token-free review.
