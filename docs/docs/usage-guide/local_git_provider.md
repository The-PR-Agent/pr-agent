---
title: "Local Git Provider"
sidebar_position: 13
---

Use the `local` Git provider to run PR-Agent from a local Git checkout when there is no hosted GitHub, GitLab, or Bitbucket pull/merge request. It treats the local branch comparison as a pull-request-like change and writes the result to files in the checkout.

This page covers branch-to-branch local review. To run PR-Agent locally against an existing hosted PR/MR, follow [Run from source](../installation/locally.md#run-from-source) and configure that hosted provider instead.

## When to use it

Use the Local Git Provider for:

- local experimentation
- workflows without a hosted PR/MR
- CI jobs that operate directly on a local Git checkout

## How it works

- The current `HEAD` is the proposed change.
- PR-Agent computes the diff between the current `HEAD` and the merge base of `HEAD` and the supplied local target branch.

The shared CLI keeps the `--pr_url` option name for compatibility with the other providers. With `git_provider=local`, pass a local target branch name such as `main`, not a hosted PR/MR URL.

## Prerequisites

Before running a command:

- Run it from inside the Git repository that contains the proposed change.
- Keep the working tree clean. Check it with `git status --short`; the command compares committed `HEAD` content.
- Make sure the target branch exists locally, for example with `git branch --list main`.
- Configure the LLM provider required by your setup. A hosted Git provider token is not required for this mode.

## Basic usage

The CLI command names are `review` and `describe`; they correspond to the `/review` and `/describe` commands. `improve` runs `review`.

Run `/review` against a local `main` branch:

```bash
cd /path/to/repository
CONFIG__GIT_PROVIDER=local \
python -m pr_agent.cli --pr_url main review
```

Run `/describe`:

```bash
CONFIG__GIT_PROVIDER=local \
python -m pr_agent.cli --pr_url main describe
```

Replace `main` with the local branch to use as the target.

## Output files

By default, the commands write files in the repository root:

- The `/review` command writes `review.md`.
- The `/describe` command writes `description.md`.

The local provider writes these files instead of publishing the result to a hosted pull/merge request.

## Custom output paths

Set paths in a trusted, operator-controlled configuration file outside the repository under review, for example `$HOME/.config/pr-agent/local.toml`:

```toml
[local]
review_path = "artifacts/review.md"
description_path = "artifacts/description.md"
```

The local provider does not automatically load the repository's `.pr_agent.toml`. `--extra_config_url` loads trusted host configuration without the repository-settings filter, so do not point it at a file from an untrusted PR checkout. Pass your operator-controlled file before the command:

```bash
CONFIG__GIT_PROVIDER=local \
python -m pr_agent.cli --pr_url main --extra_config_url "$HOME/.config/pr-agent/local.toml" review
```

When using relative custom paths, run PR-Agent from the repository root as shown above.

Create any parent directories for custom paths before running the command.

## Limitations

The Local Git Provider cannot publish to a hosted platform: it does not publish hosted PR comments, apply labels, add reactions, or publish inline comments.

PR-Agent still requires a configured LLM provider, which may require network access.
