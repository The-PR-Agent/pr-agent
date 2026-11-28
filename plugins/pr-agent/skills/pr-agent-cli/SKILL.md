---
name: pr-agent-cli
description: >-
  Run PR-Agent from the command line against a hosted PR/MR (GitHub, GitLab,
  Bitbucket, Gitea, Azure DevOps) or a local branch: review, describe,
  improve, ask, add_docs, generate_labels, similar_issue, via Docker, pip or
  uv, including saving a review to a file without publishing. Use when asked
  to get an AI review or description of a pull request or local branch with
  PR-Agent.
---

# PR-Agent CLI

References: `docs/docs/installation/locally.md`,
`docs/docs/usage-guide/local_git_provider.md`, `docs/docs/commands/`.

You need an LLM key (e.g. `OPENAI__KEY`) and, for hosted PRs, a Git token
with repo scope.

## Docker

```bash
docker run --rm -it -e OPENAI__KEY=$OPENAI_KEY -e GITHUB__USER_TOKEN=$GITHUB_TOKEN \
  pragent/pr-agent:latest --pr_url <pr_url> review

docker run --rm -it -e OPENAI__KEY=$OPENAI_KEY -e CONFIG__GIT_PROVIDER=gitlab \
  -e GITLAB__PERSONAL_ACCESS_TOKEN=$GITLAB_TOKEN pragent/pr-agent:latest --pr_url <mr_url> describe

docker run --rm -it --env-file .env pragent/pr-agent:latest --pr_url <pr_url> <command>
```

Any setting can be passed as an env var `<TABLE>__<KEY>=<VALUE>`, e.g.
`GITHUB__BASE_URL=https://github.mycompany.com/api/v3` for GHES.

## pip / uv

```bash
pip install "pr-agent[github]"    # extras: github, gitlab, bitbucket, azure, codecommit, gitea, google, all
uv run pr-agent --pr_url <pr_url> review
uv run pr-agent --pr_url <pr_url> ask "What is the purpose of this PR?"
uv run pr-agent --issue_url <issue_url> similar_issue   # needs: uv sync --group similar-issue
```

From source, copy `pr_agent/settings/.secrets_template.toml` to
`.secrets.toml` (chmod 600) and fill it in.

## Commands

| Command | Does |
|---------|------|
| `describe` | Title, type, summary, walkthrough, labels |
| `review` | Issues, security concerns, tests, effort |
| `improve` | Runs review; findings can carry committable fixes |
| `ask "..."` | Answers questions about the PR |
| `add_docs` | Docs for undocumented components |
| `generate_labels` | Custom labels from the diff |
| `update_changelog` | Updates CHANGELOG.md |

Per-command overrides go after the command:
`review --pr_reviewer.extra_instructions='be concise please'`.

## Don't publish by accident

Hosted commands post comments/labels on the PR. To inspect privately:

```bash
uv run pr-agent --pr_url https://github.com/owner/repo/pull/123 --output review.md review
```

## Local branch (no hosted PR)

```bash
cd repo && git status --short   # must be clean; compares committed HEAD
CONFIG__GIT_PROVIDER=local python -m pr_agent.cli --pr_url main review    # writes review.md
CONFIG__GIT_PROVIDER=local python -m pr_agent.cli --pr_url main describe  # writes description.md
```

`--pr_url` takes the local target branch here. Shallow clones need enough
history for `git merge-base HEAD main`.

## Errors

litellm errors like `APIError: OpenAIException - Connection error.` almost
always mean a wrong key, token or provider setting.
