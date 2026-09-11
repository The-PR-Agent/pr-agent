# PR-Agent Dashboard — S3 (run control), S4 (configuration editor), S5 (findings index) design

Completes the five-subsystem decomposition begun in
`docs/superpowers/specs/2026-09-10-pr-dashboard-s1-s2-design.md`. S1 and S2 shipped in
PR #2 on `feature/pr-dashboard`; this spec covers the remaining three. It assumes that
work as its foundation and does not restate it.

## Decisions taken

| Decision | Choice | Why |
| --- | --- | --- |
| Run execution | Subprocess per run, invoking the `pr-agent` CLI | Isolated from the web server: a hung or crashing run cannot take the dashboard down, can be force-killed, and streams real logs. In-process would share Dynaconf settings state with the server |
| TOML writes | Add `tomlkit` as a runtime dependency | The recorded S4 guardrail requires comments and section order to survive a write, and `AGENTS.md` forbids reformatting these files. Stdlib `tomllib` is read-only |
| Branch | `feature/pr-dashboard-s3-s4-s5`, stacked on `feature/pr-dashboard` | PR #2 stays reviewable on its own; this work does not grow it |
| Findings index freshness | Reuse the existing `provider_cache` TTL and stale labelling | S5 is a fan-out over the same provider reads S1 already caches; a second caching mechanism would be two sources of truth |

`tomlkit` is the first new runtime dependency this feature has taken. It is pure Python,
has no transitive dependencies, and is used by Poetry and pdm. Approved by the user
before this spec was written.

## Binding constraints inherited from S1+S2

These are not restated per-subsystem below; they bind everything in this spec.

- Python ≥ 3.12, `uv run` always, `PYTHONPATH=. uv run pytest <path> -q`.
- 120-character lines, double quotes, Ruff E/F/B/I, nothing added to `lint.ignore`.
- Local single-user deployment, no authentication. Credentials stay in `.secrets.toml`
  and environment variables and are never written, logged, or templated.
- Costs are `Decimal` end to end, stored as TEXT. NULL or unparsable means "could not be
  priced", never "free".
- All new code lives in `pr_dashboard/`. **The `pr_agent/` footprint stays at the two
  files S1+S2 already changed.** Nothing in this spec adds a third.
- House test style: `class TestX:` with a one-line docstring per test method.
- Every test must fail against a gutted implementation. This was the recurring defect
  across S1+S2 — ten instances — and the standard is unchanged.

---

## A. S3 — Run control

### Execution model

One subprocess per run: `uv run pr-agent --pr_url <url> <command>`, launched with `cwd`
set to the repository root, `stdout` and `stderr` merged into a pipe.

A run is identified by a UUID the dashboard generates before launching. That UUID is
passed to the child as the `PR_DASHBOARD_RUN_TOKEN` environment variable.

**Why a token rather than matching on `(pr_url, command, time)`:** the dashboard needs to
join the invocation it launched to the accounting row the recorder writes from inside the
child process. Matching on a time window is guesswork and breaks under concurrency. The
token makes the join exact.

Reading that variable and storing it is a change to `pr_dashboard/recorder.py` and a new
nullable `dashboard_token` column on `runs` — both inside `pr_dashboard/`. The
`pr_agent/` footprint is unchanged.

### Invocation table

A new `ui_runs` table records the invocation. It is deliberately separate from `runs`:

- `runs` is accounting, written by the recorder from inside the child, and exists only
  when `pr_dashboard.record_runs` is enabled.
- `ui_runs` is invocation state — pid, exit code, log path, cancellation — and must exist
  whether or not recording is on, because the run page has to work either way.

```sql
CREATE TABLE IF NOT EXISTS ui_runs (
    token        TEXT PRIMARY KEY,     -- the UUID passed as PR_DASHBOARD_RUN_TOKEN
    provider     TEXT NOT NULL,
    repo_slug    TEXT NOT NULL,
    pr_number    INTEGER,
    pr_url       TEXT NOT NULL,
    command      TEXT NOT NULL,        -- one of ALLOWED_COMMANDS, never free text
    status       TEXT NOT NULL,        -- queued | running | ok | failed | cancelled
    pid          INTEGER,
    exit_code    INTEGER,
    log_path     TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS ui_runs_started ON ui_runs(started_at DESC);
```

A `queued` row is written **before** the process is spawned, so a crash between the
decision to run and the spawn leaves an auditable row rather than nothing.

### Safety

The dashboard builds a command line, which makes this the most dangerous surface in the
whole feature. All of the following are required:

- **Never use a shell.** `subprocess.Popen` with an argument list, `shell=False`. No
  string interpolation into a command line anywhere.
- **Whitelist the command.** `ALLOWED_COMMANDS = ("review", "improve", "describe",
  "ask")` — a fixed tuple, membership-checked, never passed through from a request.
  `ask` takes free-text; it is passed as a single separate argv element, never
  concatenated.
- **Validate the PR URL** against the registered repository before launching: it must
  parse to a `(provider, slug, number)` whose `(provider, slug)` is in the registry.
  A URL for an unregistered repo is rejected. This stops the dashboard being used to run
  PR-Agent against arbitrary third-party repositories.
- **Cap concurrency** at `MAX_CONCURRENT_RUNS = 2`. A request beyond the cap is refused
  with a page-level message, not queued indefinitely.
- **Timeout** each run at `RUN_TIMEOUT_SECONDS = 1800`, after which it is killed and
  recorded as `failed` with a message saying it timed out.

### Logs

Each run writes to `~/.pr_dashboard/logs/<token>.log`. The run page tails the file rather
than holding output in memory, so a long run cannot grow the server's heap.

**Log output can contain secrets.** PR-Agent logs settings and provider interactions, and
a token can appear in a traceback or a debug line. Therefore:

- Log files are written with mode `0600`.
- The run detail view passes log text through a redaction pass before rendering: any
  value that matches a known credential setting (`GITHUB.USER_TOKEN`,
  `BITBUCKET.BEARER_TOKEN`, `OPENAI.KEY`, and the rest of the configured secrets) is
  replaced with `***`. Redaction is applied on read, not on write, so a rotated
  credential cannot leave an unredacted historical log readable.

### Routes

| Route | Purpose |
| --- | --- |
| `GET /runs` | Recent invocations, newest first, with status |
| `POST /runs` | Start a run. Form fields `provider`, `slug`, `number`, `command` |
| `GET /runs/{token}` | Detail: status, redacted log tail, and the accounting row if recording is on |
| `POST /runs/{token}/cancel` | Terminate a running process |

`GET /runs/{token}` is the HTMX polling target while a run is `running`; polling stops
once the status is terminal.

---

## B. S4 — Configuration editor

### Editable set

Discovered, never free-form. Three groups:

1. `.pr_agent.toml` at the repository root — the per-repository override, created if absent.
2. `pr_agent/settings/configuration.toml` — the shipped defaults.
3. `pr_agent/settings/*_prompts.toml` and `pr_agent/settings/code_suggestions/*.toml` —
   the prompt files.

The editable set is computed by globbing those locations at request time and is the
**only** thing a request may name. A request identifies a file by its index in that
discovered set or by a path that must be `==` to a discovered path after resolution —
never by a path fragment joined onto a base directory. This is what closes path
traversal; there is no sanitising of `..`, because no caller-supplied path is ever joined.

`.secrets.toml` and `settings_prod/` are **excluded from discovery entirely**. They hold
credentials, and the design principle everywhere else in this feature is that the
dashboard never reads a credential. A user who needs to change a secret edits the file.

### The write path

Every write goes through the same four steps, in order, and the recorded guardrails
require all four:

1. **Parse and validate.** The submitted text must parse as TOML (`tomlkit.parse`).
   Invalid input is rejected with the parse error shown; nothing is written.
2. **Diff and confirm.** The user is shown a unified diff of current versus submitted and
   must confirm explicitly. A `POST` that arrives without a confirmation token matching
   the previewed content is refused — this also prevents a stale preview being applied to
   a file that changed underneath it.
3. **Back up.** The previous content is copied to
   `~/.pr_dashboard/backups/<ISO-timestamp>/<relative path>` before the new content lands.
4. **Write to the working tree only.** Never `git add`, never `git commit`. The user
   reviews the change with their own tools.

Round-tripping is `tomlkit`'s job: the editor submits full file text, and validation
parses it, so comments and ordering survive because the user's own text is what gets
written. `tomlkit` is used for validation and for programmatic single-value edits; it is
not used to re-serialise a whole file the user hand-edited, because re-serialising would
be exactly the reformatting `AGENTS.md` forbids.

### Why editing prompts is riskier than editing settings

A malformed prompt does not fail loudly — it produces worse reviews. The editor therefore
shows, on every prompt file, that the file is a prompt and that Jinja2 renders it with
`StrictUndefined`, so a variable that is referenced but not supplied raises at render
time rather than being silently empty. Validation checks that the submitted text still
compiles as a Jinja2 template, which catches an unclosed block before it reaches a run.

### Routes

| Route | Purpose |
| --- | --- |
| `GET /config` | The discovered editable set, grouped |
| `GET /config/{index}` | Editor for one file, with its current content |
| `POST /config/{index}/preview` | Validate, return a diff and a confirmation token |
| `POST /config/{index}` | Write, given a matching confirmation token |
| `GET /config/backups` | List backups, newest first, with a restore action |

---

## C. S5 — Cross-repo findings index

### What it does

For every registered repository, list its open pull requests, fetch PR-Agent's review
comments on each, parse their findings, and present one filterable table across all of
them. This is the "see all the repos and data in one dashboard" view.

### Cost, and why it is bounded

This is a fan-out: repositories × open PRs × comment fetches. Against ten repositories
with twenty open PRs each, a naive implementation makes hundreds of API calls per page
load and will hit rate limits.

Bounding rules:

- Reuse `providers.cached`, so a repeat view inside the TTL costs nothing. The existing
  stale labelling applies unchanged.
- Cap at `MAX_INDEXED_PRS_PER_REPO = 20`, newest first, and state on the page that the
  view covers the most recent N per repository rather than implying completeness.
- Fetch repositories sequentially, not concurrently. Provider rate limits are the binding
  constraint, not wall-clock; a burst of parallel requests buys little and risks a 429
  that costs the whole page.
- A per-repository `ProviderError` degrades that repository only: its rows are missing,
  an attributed message says so, and the rest of the table still renders. This mirrors the
  attributed per-repo error handling already built for the overview.

### Filters

Repository, command that produced the finding (`review` vs `improve`), whether the
finding has a file location, and a free-text substring match on the title. Filtering
happens in Python over the assembled list, not in SQL, because the findings are parsed
from live provider data and were never in the database.

### Route

`GET /findings`, with filters as query parameters, rendering a table of
repository / PR / title / file / lines, each row linking to the existing PR detail page.

---

## Testing

Beyond the inherited standard, three areas need tests that would be easy to write
vacuously and must not be:

- **S3 must never execute a real `pr-agent` run in the suite.** Stub the subprocess
  launcher. Add a test asserting `shell=False` and that the argv list is built from the
  whitelist — a test that only checks the happy path would not catch a shell injection
  regression. Include a test that a command outside `ALLOWED_COMMANDS` and a PR URL for
  an unregistered repository are both refused.
- **S3 redaction** needs a test that seeds a log file containing a known fake token and
  asserts it does not appear in the rendered response — asserting `***` appears is not
  sufficient, because it would pass on a page that rendered nothing.
- **S4 must never write outside the discovered set.** Test a request naming a path
  outside it, and a request naming `.secrets.toml`, and assert both are refused and no
  file changed on disk. Test that a write creates a backup whose content equals the
  previous file, and that the file's comments survive a round trip.

## Risks

| Risk | Mitigation |
| --- | --- |
| A UI that runs shell commands is the highest-value target in the feature | No shell, argv lists only, command whitelist, PR URL validated against the registry |
| A provider token leaking through a run log | 0600 log files, redaction on read against the configured secret set |
| The config editor corrupting a prompt file that PR-Agent needs at runtime | Parse-and-Jinja-validate before write, diff plus explicit confirmation, timestamped backup, working tree only |
| S5 exhausting a provider rate limit on one page load | Reuse the existing TTL cache, cap PRs per repository, fetch sequentially, degrade per repository |
| `tomlkit` re-serialisation reformatting a file | `tomlkit` validates and does targeted edits; whole-file writes use the user's own submitted text |

## Out of scope

Authentication, multi-user, and remote deployment remain out of scope, as in S1+S2. So
does scheduling runs, diffing findings across time, and editing `.secrets.toml`.
