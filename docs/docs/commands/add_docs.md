---
title: "Add Docs"
sidebar_position: 6
---

## Overview

The `add_docs` command scans the PR code changes and suggests documentation for any code components that are missing documentation, such as functions, classes, and methods.

It can be invoked manually by commenting on any PR:

```
/add_docs
```

## Example usage

Invoke the command manually by commenting `/add_docs` on any PR:

<img src="/img/add_docs_comment.png" alt="Add Docs" width="512" />

The command will generate documentation suggestions as inline code suggestions.

### Export documentation proposals as JSON

To review or consume documentation proposals in a script or CI job without
publishing PR suggestions, run:

```bash
pr-agent --pr_url https://github.com/org/repo/pull/123 --json-output docs.json add_docs \
  --config.publish_output=false
```

You can also analyze a standalone diff, even without a working tree:

```bash
git diff main...HEAD | pr-agent --stdin --json-output docs.json add_docs
```

The JSON contains a `documentation_proposals` list and the run's token `usage`
(empty when unavailable). Each proposal contains `relevant_file`,
`relevant_line` (a positive integer), `doc_placement` (`before` or `after`),
and `documentation`. Only structurally usable, non-blank proposals are
included. An empty result or a diff with no analyzable files writes an empty list.

These are model-proposed locations and documentation, not validated edits or
confirmation of publication. File paths retain the model-reported text, and
the line and indentation must be checked before applying any proposal.
Unlike inline suggestions, the JSON draft does not require complete source
files. It may contain proposals that cannot be published as inline suggestions,
and does not guarantee that the entire PR was analyzed.

The JSON is written before permanent suggestion publication. An export failure
stops publication and a failed or policy-skipped run exits nonzero; check the
exit status before consuming the artifact because a failed run does not remove
a previous file. Without `--json-output`, existing behavior is unchanged.

### Language-specific documentation styles

The command automatically detects the programming language and generates documentation in the appropriate format:

| Language | Documentation Format |
|----------|---------------------|
| Python | Docstrings (Sphinx, Google, Numpy styles) |
| Java | Javadocs |
| JavaScript/TypeScript | JSdocs |
| C++ | Doxygen |
| Other | Generic documentation |

## Configuration options

Under the section `[pr_add_docs]`, the following options are available:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `extra_instructions` | string | `""` | Additional instructions for the AI model |
| `docs_style` | string | `"Sphinx"` | Documentation style for Python. Options: `"Sphinx"`, `"Google Style with Args, Returns, Attributes...etc"`, `"Numpy Style"`, `"PEP257"`, `"reStructuredText"` |
| `use_repo_context` | boolean | `true` | Apply conventions from configured repository instruction files (for example `AGENTS.md`); set to `false` to opt out |

### Example configuration

To customize the documentation style, add the following to your configuration file:

```toml
[pr_add_docs]
docs_style = "Google Style with Args, Returns, Attributes...etc"
extra_instructions = "Focus on documenting public methods and include usage examples"
```

By default, `/add_docs` follows the shared `config.repo_context_files` selection (`["AGENTS.md"]` by default,
read from the repository default branch). To disable repository instructions for `/add_docs` only:

```toml
[pr_add_docs]
use_repo_context = false
```

Structured sibling-repository entries in `config.repo_context_files` are excluded from `/add_docs` to avoid publishing private sibling guidance in PR suggestions. Local repository entries remain in effect.

Setting `config.repo_context_files = []` disables repository instructions across commands. Repository instructions
are optional prompt context: if they do not fit the selected model's input budget, they are omitted without shortening
the PR diff. Explicit `extra_instructions` and `docs_style` take precedence. Repository instructions may add file
reads and prompt tokens, but do not introduce another model call.
Repository instructions are appended to the rendered system prompt, including with custom `[pr_add_docs_prompt]`
templates. Existing template variables and diff rendering are left unchanged.

### Command line options

You can pass configuration options directly in the command:

```
/add_docs --pr_add_docs.docs_style="Numpy Style"
```

## How it works

1. The command analyzes the PR diff to identify code components (functions, classes, methods) that lack documentation
2. It uses AI to generate appropriate documentation based on the code context and language
3. Documentation suggestions are published as inline code suggestions that can be applied with a single click

### Model response failures

Each model response is validated before it is accepted. Missing content or an
invalid documentation structure triggers the next model in `CONFIG.FALLBACK_MODELS`,
if configured. An explicit empty list or `Code Documentation: null` is a valid
empty result and does not trigger another model call.

If all model attempts fail, PR-Agent records the command as failed and removes
the temporary progress comment. With `CONFIG.PUBLISH_OUTPUT` enabled, it also
attempts to post **Failed to generate code documentation for this PR.**

### Publication failures

Suggestions are skipped if their original source line cannot be reliably retained,
including when file content is unavailable or incomplete. Valid suggestions in the
same response can still be published.

With `CONFIG.PUBLISH_OUTPUT` enabled, PR-Agent retries unsuccessful batch
publication one suggestion at a time. If the batch and every individual retry
explicitly report failure, it attempts to post **Failed to publish code
documentation for this PR.** and records the command as failed. Partial success
or an unconfirmed provider result does not trigger this all-failed outcome.
