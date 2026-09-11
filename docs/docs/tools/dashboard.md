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

Costs come from litellm's synchronous pricing. Where a model has no pricing entry, or the
provider did not report usage, the dashboard shows "not reported" rather than `$0.00`.

**`record_runs` defaults to `false`.** If the **Usage** page is empty, this is almost
always why: no runs have been recorded yet because the setting was never turned on. Set
it to `true` and run a command against a PR to start populating `~/.pr_dashboard/usage.db`.

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `pr_dashboard.record_runs` | `false` | Record one usage row per command run |
