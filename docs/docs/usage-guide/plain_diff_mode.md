# Plain-diff local mode

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
| `--output <path>` | Write the result to a file in addition to stdout |

`--stdin` and `--diff-file` are mutually exclusive. At least one must be provided to
enter plain-diff mode; omitting both falls back to the normal `--pr_url` flow.

### Supported commands

`review`, `improve`, `describe`, and `ask` are supported. Because there is no hosting
platform to push to, `improve` renders its code suggestions as a single markdown
document to stdout (and to `--output`, if given) instead of as committable inline
suggestions. Commands that require live platform interaction (such as
`update_changelog` or `similar_issue`) are not meaningful in this mode.

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
   also written to that file (UTF-8, overwritten on each run).

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
