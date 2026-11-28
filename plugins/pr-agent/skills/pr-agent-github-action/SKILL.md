---
name: pr-agent-github-action
description: >-
  Add PR-Agent to a GitHub repository as a GitHub Action: the workflow file,
  required permissions and secrets, env-based configuration, synchronize and
  review_requested triggers, and the security trade-offs of
  pull_request_target for fork PRs. Use when setting up automatic PR reviews
  in GitHub Actions.
---

# PR-Agent as a GitHub Action

Reference: `docs/docs/installation/github.md`,
`docs/docs/usage-guide/automations_and_usage.md`.

## Workflow

`.github/workflows/pr_agent.yml`:

```yaml
on:
  pull_request:
    types: [opened, reopened, ready_for_review]
  issue_comment:
jobs:
  pr_agent_job:
    if: ${{ github.event.sender.type != 'Bot' }}
    runs-on: ubuntu-latest
    permissions:
      contents: read
      issues: write
      pull-requests: write
    steps:
      - name: PR Agent action step
        uses: the-pr-agent/pr-agent@main
        env:
          OPENAI_KEY: ${{ secrets.OPENAI_KEY }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

- Add the `OPENAI_KEY` repository secret. `GITHUB_TOKEN` exists already but
  must be passed through `env`; without it, targeted events fail with
  `GITHUB_TOKEN not set`.
- `issue_comment` lets people trigger `/review`, `/describe`, `/ask ...` in
  comments.
- Consider pinning `uses:` to a release tag or SHA instead of `@main`.

## Configure through env

Any key from `pr_agent/settings/configuration.toml`:

```yaml
env:
  PR_REVIEWER.REQUIRE_TESTS_REVIEW: "false"
  PR_REVIEWER.NUM_MAX_FINDINGS: 5
```

## Triggers

- To process new pushes, add `synchronize` to the workflow's
  `on.pull_request.types` list. Then add it to
  `github_action_config.pr_actions`, or enable
  `github_action_config.handle_push_trigger` for `push_commands`.
- With both `opened` and `review_requested` enabled, the initial
  `review_requested` is skipped so commands run once.

## Fork PRs

`pull_request` from forks gets no secrets and a read-only token, so reviews
fail. `pull_request_target` gives access but runs with secrets on untrusted
contributions. Explain that trade-off to the user before switching, and
never check out and execute fork code in such a workflow.
