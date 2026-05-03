# Config-Driven Repository Context Files

## Goal

PR-Agent should support explicitly configured repository context files, such as `AGENTS.md`, and inject their contents into the AI prompts for PR analysis. This gives the model stable project-level context about repository purpose, architecture, and conventions without requiring teams to duplicate that information into every tool's `extra_instructions`.

## Non-Goals

- Do not automatically read `AGENTS.md` by default.
- Do not replace existing `extra_instructions` behavior.
- Do not fail PR commands because an optional context file is missing or unreadable.
- Do not add broad repository indexing or retrieval.

## Configuration

Add two config keys under `[config]`:

```toml
repo_context_files = []
repo_context_max_lines = 500
```

`repo_context_files` is an ordered list of repository-relative file paths to load from the target repository's default branch. An empty list preserves current behavior.

`repo_context_max_lines` caps the total rendered context across all configured files. This limits prompt growth and keeps the feature predictable.

Example:

```toml
[config]
repo_context_files = ["AGENTS.md", "CONTRIBUTING.md"]
repo_context_max_lines = 300
```

## Data Flow

1. Repository settings are applied as they are today.
2. Tool initialization asks the git provider for configured context files.
3. The provider fetches file contents from the repository's default branch where supported.
4. PR-Agent formats the loaded content with clear file headers.
5. Prompt variables include the formatted value as `repo_context`.
6. Review, describe, and improve prompts render a `Repository context` block only when `repo_context` is non-empty.

## Provider Interface

Add a provider method for fetching arbitrary repository files by path from the default branch. The first implementation should support GitHub because this repo's existing `get_repo_settings` path already reads `.pr_agent.toml` from the default branch through PyGithub.

Providers that do not implement the method should return empty content through the base implementation. Missing files should be logged and skipped.

## Prompt Behavior

`repo_context` should be separate from `extra_instructions`. Extra instructions remain user-authored prompt directives. Repository context is background information the model should consider when evaluating the PR.

The prompt block should be concise:

```text
Repository context:
======
## AGENTS.md
...
======
```

The block will be added to:

- `/review`
- `/describe`
- `/improve`

Other tools can opt in later when there is a concrete use case.

## Error Handling

- Empty configuration: no work and no prompt changes.
- Missing file: debug or warning log, skip the file.
- Unsupported provider: no context loaded.
- Invalid path type or empty path: skip with logging.
- Max line cap exceeded: truncate after the configured total line count.

## Testing

Add focused unit tests covering:

- no configured files produces empty context
- configured files are fetched and formatted with headers
- missing files are skipped without raising
- total context respects `repo_context_max_lines`
- review, describe, and improve tool variables include `repo_context`

Use provider fakes where possible rather than network calls.
