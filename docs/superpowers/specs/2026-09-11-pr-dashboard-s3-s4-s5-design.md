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

## Cross-cutting: the dashboard is a localhost target

S1+S2 was a read-only mirror, so "local, single user, no authentication" cost nothing.
S3 and S4 change that: this becomes a service on `localhost` that executes commands and
writes files. Any web page the user visits while the dashboard is running can send it a
cross-origin request. No authentication means no cookie to steal — but also nothing
stopping a forged form POST.

Every state-changing route (`POST`, and any `GET` with a side effect — there are none, and
there must not be) therefore requires **both**:

- **Origin/Host validation.** Reject the request unless `Origin` (or `Referer` when
  `Origin` is absent) matches the configured bind address exactly, and `Host` is one of
  the expected loopback values. A missing `Origin` on a state-changing request is a
  rejection, not a pass.
- **A per-session CSRF token.** Minted into the session on first page load, embedded in
  every form, compared in constant time on submit. This is what stops a form POST from a
  page that can guess the route but cannot read the response.

Bind to `127.0.0.1` by default, never `0.0.0.0`. If a future change makes the bind address
configurable, binding off-loopback must fail unless authentication exists.

This section is not optional hardening. Without it, "local and unauthenticated" means any
site the user browses can trigger a PR-Agent run or rewrite a prompt file.

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
nullable, `UNIQUE`, `dashboard_token` column on `runs` — both inside `pr_dashboard/`. The
`pr_agent/` footprint is unchanged.

**The join is best-effort, and the UI must say so.** Accounting is opt-in
(`record_runs` defaults to false), the recorder only starts after repo settings are
applied, and the child can die before it ever runs. So an invocation with no matching
`runs` row is the *normal* case, not an error. `ui_runs` therefore carries its own
terminal state and the run page renders one of three things explicitly: the accounting
row; "accounting not enabled for this run"; or "the run ended before accounting started".
It must never show an unresolved join, a spinner that never resolves, or a zero cost
standing in for an absent row — a missing accounting row is unknown, exactly as a NULL
cost is unknown rather than free.

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
- **Encode `ask`'s free text.** A separate argv element is *not* sufficient. PR-Agent's
  CLI turns any argument beginning `--` into a settings override
  (`pr_agent/cli.py`, `--extra_config_url` and friends), so a question of
  `--pr_questions.extra_instructions="..."` becomes configuration rather than a question
  and changes how the run behaves. Pass free text through
  `pr_agent.algo.utils.encode_user_text_arg` before building argv — the same function
  `pr_agent/servers/azuredevops_server_webhook.py:137` already uses for exactly this — and
  reject any additional `ask` arguments. No other command takes free text.
- **Validate the PR URL's authority, not just its shape.** Parsing to a
  `(provider, slug, number)` whose `(provider, slug)` is in the registry is not enough:
  `https://attacker.example/owner/repo/pull/1` parses GitHub-shaped and matches a
  registered slug. Require all of: scheme is `https`; host equals the configured host for
  that provider exactly (no suffix matching — `github.com.evil.test` must fail); no
  userinfo, query, or fragment; a canonical path with no `.` or `..` segments; and the
  resolved `(provider, slug, number)` equal to the registry entry. Build the URL that gets
  passed to the child from the *registry entry plus the validated number*, never from the
  submitted string — so even a validation gap cannot put attacker text on the command line.
- **Cap concurrency** at `MAX_CONCURRENT_RUNS = 2`. A request beyond the cap is refused
  with a page-level message, not queued indefinitely.
- **Timeout** each run at `RUN_TIMEOUT_SECONDS = 1800`, after which it is killed and
  recorded as `failed` with a message saying it timed out.
- **Do not inherit the environment.** `Popen(env=...)` with an explicit allowlist, never
  the ambient environment. An inherited `PR_AGENT_EXTRA_CONFIG_URL` (read as the default
  for `--extra_config_url` at `pr_agent/cli.py:60`) would make every dashboard run fetch
  configuration from an external URL; inherited proxy and OTLP exporter variables can
  redirect the child's traffic and telemetry. Carry only: `PATH`, `HOME`, `LANG`/`LC_ALL`,
  the provider and model credentials the run actually needs, `PYTHONPATH` where the
  invocation requires it, and `PR_DASHBOARD_RUN_TOKEN`. Everything else is dropped.
  Document that dropping the ambient environment is deliberate, so a user who relies on an
  exported setting learns it from the code rather than from a silently different result.

### Logs

Each run writes to `~/.pr_dashboard/logs/<token>.log`. The run page tails the file rather
than holding output in memory, so a long run cannot grow the server's heap.

**Log output can contain secrets.** PR-Agent logs settings and provider interactions, and
a token can appear in a traceback or a debug line. Therefore:

- Log files are written with mode `0600`.
- **Reduce what reaches the log in the first place.** The child runs at default verbosity;
  the dashboard never enables PR-Agent's debug or settings-dump output. Redaction is the
  second line of defence, not the first.
- **Redact on read, not on write**, so rotating a leaked credential also protects the
  historical logs.
- **Exact-value substitution is not enough on its own**, and the spec must not pretend it
  is. A token can appear URL-encoded, truncated inside a traceback, or Base64'd in an
  `Authorization` header, and none of those match the raw setting value. So redaction runs
  three passes: exact values from the configured secret set; the common encodings of those
  values (URL-encoded, Base64); and structural patterns that look like credentials
  regardless of source (`Authorization: <scheme> <blob>`, `ghp_`/`github_pat_` prefixes,
  `sk-` prefixes, long high-entropy hex or Base64 runs).
- **Fail closed.** If the secret inventory cannot be read, the log is not rendered — a
  message says redaction is unavailable. An empty secret set must never mean "nothing to
  redact, show everything".
- The raw log stays on disk at `0600` for a user who needs it; only the rendered view is
  redacted, and the page says so rather than implying the file itself is clean.

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

The editable set is computed by globbing those locations and is the **only** thing a
request may name. A request identifies a file by its index in that set — never by a path
fragment joined onto a base directory. That closes classic traversal: no caller-supplied
path is ever joined, so there is no `..` to sanitise.

**Discovery membership is not sufficient by itself, because a discovered entry can be a
symlink.** A repository containing `.pr_agent.toml -> ~/.ssh/config` yields a discovered
file whose resolved target is outside every approved root, and writing "the discovered
file" would overwrite the target. Worse, the link can be swapped between preview and
write. So discovery additionally requires, and the write path re-checks immediately
before replacing:

- The entry is a **regular file, not a symlink** (`lstat`, not `stat`) and not a FIFO,
  device, or directory.
- Its resolved path is **beneath one of the approved roots** — the repository root or
  `pr_agent/settings/` — after full resolution.
- Its **file identity** (device plus inode, captured at preview) is unchanged at write
  time. If it changed, the write is refused and the user is told to re-preview. This is
  what defeats the swap-between-preview-and-write race that a path-only check misses.

`.secrets.toml` and `settings_prod/` are **excluded from discovery entirely**. They hold
credentials, and the design principle everywhere else in this feature is that the
dashboard never reads a credential. A user who needs to change a secret edits the file.

### The write path

Every write goes through the same four steps, in order, and the recorded guardrails
require all four:

1. **Parse and validate.** The submitted text must parse as TOML (`tomlkit.parse`).
   Invalid input is rejected with the parse error shown; nothing is written.
2. **Diff and confirm.** The user is shown a unified diff of current versus submitted and
   must confirm explicitly. The confirmation token is server-side state, not a hash the
   client hands back, and binds **all** of: the canonical resolved target path, the file
   identity (device plus inode), a hash of the original content, a hash of the submitted
   content, an expiry, and single use. A token that has been spent, has expired, or whose
   original-content hash no longer matches the file on disk is refused with a message
   saying the file changed and the preview must be redone. Without every one of those
   bindings the token is replayable, or applicable to a file that drifted after discovery.
3. **Back up, durably.** The previous content is written to
   `~/.pr_dashboard/backups/<ISO-timestamp>/<relative path>` at mode `0600`, then
   `fsync`ed, before the new content lands. A backup that was never flushed is not a
   backup. Restore verifies the backup's recorded hash before overwriting anything.
4. **Write atomically, to the working tree only.** Copy-then-overwrite can leave a
   truncated config file if the process dies or the disk fills mid-write — and a truncated
   `configuration.toml` breaks every subsequent PR-Agent run. Instead: write the new
   content to a temporary file **in the same directory**, `fsync` it, `os.replace` it over
   the target, then `fsync` the directory. `os.replace` is atomic within a filesystem, so
   a reader sees either the old file or the new one, never a partial one. Serialise writes
   per canonical path with a lock so two concurrent writes cannot interleave.
   Never `git add`, never `git commit` — the user reviews the change with their own tools.

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

A TTL cache and a per-repo cap bound the *number of calls*, but not the latency of a cold
load — and that distinction is the whole problem. Ten repositories × 20 PRs × a 30-second
provider timeout is roughly 100 minutes of a request holding a page open. A cap alone is
not a bound.

So `/findings` **never fetches synchronously**. It renders immediately from whatever is
cached and shows each repository's real state — fresh, stale, loading, or failed — while a
bounded background refresh fills the gaps and the page polls via HTMX.

Bounding rules:

- Reuse `providers.cached`, so a repeat view inside the TTL costs nothing. The existing
  stale labelling applies unchanged.
- Cap at `MAX_INDEXED_PRS_PER_REPO = 20`, newest first, and state on the page that the
  view covers the most recent N per repository rather than implying completeness.
- **Deadlines, not just caps.** A per-repository deadline and a total refresh deadline;
  whatever has not arrived is rendered as "still loading" rather than delaying the page.
- **Single-flight the cache fill.** Concurrent requests for the same repository must
  coalesce onto one in-flight fetch. Without this, a cold cache plus a page that polls is
  a self-inflicted stampede against the provider — the polling makes it worse, not better.
  Back off on failure rather than retrying every poll.
- Fetch repositories sequentially within a refresh. Provider rate limits are the binding
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
- **S4 must never write outside the approved roots.** Test a request naming a path outside
  the discovered set, a request naming `.secrets.toml`, and — the one a path-only check
  passes — a discovered entry that is a **symlink pointing outside an approved root**.
  Assert all three are refused and that the link's target is byte-unchanged on disk.
  Test that a write creates a backup whose content equals the previous file, and that
  comments and section order survive a round trip.
- **CSRF and origin checks need a negative test per state-changing route**, asserting a
  request with a foreign `Origin` and one with no CSRF token are both refused and that
  nothing happened — no process spawned, no file written. A test that only exercises the
  happy path proves nothing about the guard.

## Risks

| Risk | Mitigation |
| --- | --- |
| Any site the user visits can forge a request to an unauthenticated localhost service | Origin/Host validation plus a per-session CSRF token on every state-changing route; bind to 127.0.0.1 |
| A UI that runs commands is the highest-value target in the feature | No shell, argv lists only, command whitelist, `ask` text encoded via `encode_user_text_arg`, PR URL rebuilt from the registry entry after full authority validation |
| A run inheriting hostile configuration from the ambient environment | Explicit `env` allowlist; `PR_AGENT_EXTRA_CONFIG_URL`, proxy and exporter variables are dropped |
| A provider token leaking through a run log | 0600 log files, no debug output enabled, redaction on read across exact values, common encodings, and structural credential patterns; fail closed when the secret inventory is unreadable |
| The config editor writing outside the approved roots via a symlinked entry | Reject non-regular files and symlinks, require resolution beneath an approved root, re-check device+inode immediately before replace |
| A crash or full disk truncating a config file PR-Agent needs at runtime | Same-directory temp file, fsync, atomic `os.replace`, fsync the directory, per-path write lock, fsynced and hash-verified backup |
| S5 hanging a page load or stampeding the provider on a cold cache | Never fetch synchronously; render cached state immediately, bounded background refresh with per-repo and total deadlines, single-flight fills with backoff |
| `tomlkit` re-serialisation reformatting a file | `tomlkit` validates and does targeted edits; whole-file writes use the user's own submitted text |

## Out of scope

Authentication, multi-user, and remote deployment remain out of scope, as in S1+S2. So
does scheduling runs, diffing findings across time, and editing `.secrets.toml`.
