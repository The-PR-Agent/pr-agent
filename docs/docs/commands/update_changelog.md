---
title: "Update Changelog"
sidebar_position: 11
---

## Overview

The `update_changelog` command automatically updates a Markdown changelog with the PR changes.
It uses `CHANGELOG.md` at the repository root by default; `pr_update_changelog.changelog_path` selects a different file.
It can be invoked manually by commenting on any PR:

```
/update_changelog
```

Empty or fence-only model responses are retried with the configured fallback models. If no model returns usable content, the command stops without writing to the changelog or publishing an update comment.

Host operators can also opt in to forwarding the newly generated entry to the existing
[push outputs](../usage-guide/push_outputs.md) destinations by selecting `update_changelog` in
`push_outputs.commands`. These notifications contain a draft, not confirmation of a repository write.

## Example usage

<img src="/img/update_changelog_comment.png" alt="update_changelog_comment" width="768" />

<img src="/img/update_changelog.png" alt="update_changelog" width="768" />

## Configuration options

Under the section `pr_update_changelog`, the [configuration file](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml) contains options to customize the 'update changelog' command:

- `changelog_path`: repository-relative path to the Markdown changelog, such as `HISTORY.md` or `docs/CHANGELOG.md`. Default is `CHANGELOG.md`. Set it in the root `.pr_agent.toml` or host configuration; comment/CLI arguments and per-directory settings cannot override it.
- `push_changelog_changes`: whether to push the changes to the selected changelog, or just publish them as a comment. Default is false (publish as comment). Before pushing, the command requires a confirmed read of that file; a confirmed missing file is treated as empty, while other read failures skip the repository write, attempt to publish a not-pushed fallback comment, and surface the original error. If the repository write itself fails, the command makes one best-effort attempt to publish the generated changelog as a fallback and surfaces the original write error. Because a transport failure may happen after a completed remote write, this fallback reports the repository update as unconfirmed rather than promising it was not pushed.
- `extra_instructions`: Optional extra instructions to the command. For example: "Use the following structure: ..."
- `add_pr_link`: whether the model should try to add a link to the PR in the changelog. Default is true.
- `skip_ci_on_push`: whether the commit message (when `push_changelog_changes` is true) will include the term "[skip ci]", preventing CI tests to be triggered on the changelog commit. Default is true.

### Custom changelog location

For a changelog stored under `docs/`, add this to the repository's root `.pr_agent.toml`:

```toml
[pr_update_changelog]
changelog_path = "docs/CHANGELOG.md"
```

Then invoke `/update_changelog` as usual. The command reads that file to follow its existing structure and,
when `push_changelog_changes` is enabled, prepends the new entry to the same file on the PR branch.
Comment-only output also uses the selected file as context and names it in the commit instructions.

The path must be a non-empty repository-relative file path using `/` separators, without empty,
`.` or `..` components, Windows drive prefixes, backslashes, or control characters. Invalid values stop
the command before reading repository content. Spaces and Unicode names are accepted and kept exactly as written. The command updates
one Markdown changelog per command; it does not discover files, expand globs, or select a file per package.
The filename need not end in `.md`, but the generated content remains Markdown.
