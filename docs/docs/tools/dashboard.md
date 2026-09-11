## Overview

`pr-dashboard` is a local, single-user web interface for PR-Agent. It lists the
repositories you connect, shows the reviews PR-Agent has posted on their pull requests,
and reports token and cost consumption per run.

## Running it

```
uv run pr-dashboard
```

Then open `http://127.0.0.1:8420`.

## Connecting repositories

Add repositories on the **Repositories** page as `owner/name`, choosing `github` or
`bitbucket`. Credentials are **not** stored by the dashboard: it reads the same
`.secrets.toml` and environment variables PR-Agent already uses, and the page reports
whether a usable credential is configured per provider.

The registry itself lives in `~/.pr_dashboard/pr_dashboard.toml`.

## Recording usage

Consumption reporting is opt-in, because webhook and serverless deployments must not
begin writing a database merely because the package is installed. Enable it in
`.pr_agent.toml`:

```toml
[pr_dashboard]
record_runs = true
```

Each run then records one row — command, model, prompt and completion tokens, cost,
duration, and status — into `~/.pr_dashboard/usage.db`. Runs that fail before reaching a
tool are recorded as attempts with no usage, so the counts are not silently short.

When recording is enabled, SQLite lock contention can delay a command by up to the store's five-second busy timeout.

Costs come from litellm's synchronous pricing. Where a model has no pricing entry, or the
provider did not report usage, the dashboard shows "not reported" rather than `$0.00`.

### By-model cost and fallback runs

The **Usage** page's By-model breakdown groups by the last model a run used. A run that
fell back to a different model after an earlier attempt failed attributes its *entire*
cost to that final model, not split across every model it actually called during the
run. The **Totals** line's `fallback_runs` count tells you how many runs in the window
fell back, so you can judge how much this skews the By-model numbers; a per-model split
is not implemented yet.

**`record_runs` defaults to `false`.** If the **Usage** page is empty, this is almost
always why: no runs have been recorded yet because the setting was never turned on. Set
it to `true` and run a command against a PR to start populating `~/.pr_dashboard/usage.db`.

## Runs

The **Runs** page launches PR-Agent commands (`review`, `improve`, `describe`, `ask`) as
subprocesses. Each run uses a **reduced environment**: only `PATH`, `HOME`, `LANG`/`LC_ALL`,
`PYTHONPATH`, the provider and model credentials the run needs, and `PR_DASHBOARD_RUN_TOKEN`
are passed to the child. An exported variable in your shell — including
`PR_AGENT_EXTRA_CONFIG_URL`, proxy settings, or OTLP exporter endpoints — does **not**
reach a dashboard-launched run unless it is one of those allowlisted names.

Enable `record_runs` (see **Recording usage** above) if you want the run page to join
against the accounting row the child writes. Without it, the page still shows invocation
status and a redacted log tail, but reports that accounting was not enabled for the run.

## Configuration editor

The **Config** page edits `.pr_agent.toml`, `pr_agent/settings/configuration.toml`, and
prompt files discovered under `pr_agent/settings/`. Every write goes to the **working tree
only** — the dashboard never runs `git add` or `git commit`. Review changes with your own
tools before committing.

Before replacing a file, the previous content is backed up under
`~/.pr_dashboard/backups/<ISO-timestamp>-<id>/<relative path>` at mode `0600`
(one directory per backup, so two files saved in the same second cannot collide). The
**Config → Backups** page lists these snapshots and can restore one after verifying its
hash.

## Findings index

The **Findings** page aggregates review findings across every registered repository. It
never blocks on a provider: the page renders immediately from cache, shows each
repository as fresh, stale, loading, or failed, and refreshes in the background while any
repository is still loading.

The view covers the **most recent 20 open pull requests per repository** (newest update
first), not every open pull request. A repository with dozens of open PRs will not show
findings from older ones until they appear in that window.

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `pr_dashboard.record_runs` | `false` | Record one usage row per command run |
