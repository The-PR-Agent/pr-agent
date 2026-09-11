# PR-Agent Dashboard S3+S4+S5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Complete the dashboard: trigger PR-Agent runs from the UI, edit its configuration
and prompt files safely, and see findings across every registered repository in one table.

**Architecture:** Three subsystems on top of the shipped S1+S2 package. Runs execute as
subprocesses with a hardened argv and a minimal environment. Config edits go through a
validate → preview → confirm → backup → atomic-replace pipeline. The findings index never
fetches synchronously; it renders cached state and refreshes in the background.

**Tech Stack:** FastAPI, Jinja2, HTMX, Alpine, SQLite, `tomlkit`. No build step.

**Spec:** `docs/superpowers/specs/2026-09-11-pr-dashboard-s3-s4-s5-design.md` — read it.
The spec is the binding authority; where this plan and the spec disagree, the spec wins.

## Global Constraints

Every task's requirements implicitly include this section.

- Python ≥ 3.12. Always `uv run`; **never** bare `python3` (system python is 3.9 and fails).
- Tests: `PYTHONPATH=. uv run pytest <path> -q`.
- 120-character lines, double quotes. Ruff E/F/B/I; **never** add to `lint.ignore`.
- The only new runtime dependency is `tomlkit`, already added. Add nothing else.
- **Do not modify anything under `pr_agent/`.** The two files S1+S2 changed are the entire
  permitted footprint. Import from `pr_agent` freely; edit nothing.
- Do not reformat or reorder existing files.
- House test style: `class TestX:` with a **one-line** docstring per test method.
- Costs are `Decimal` end to end, stored as TEXT. NULL or unparsable means "could not be
  priced", never "free".
- Never write, log, or template a provider token.
- New routes go **inside** `create_app` in `pr_dashboard/app.py`, like every existing
  route, so per-instance isolation holds. `test_two_apps_do_not_share_state` must keep
  passing.
- Every test must fail against a gutted implementation. Verify by reverting the code under
  the test and watching it fail. This was the recurring defect in S1+S2 — ten instances.

## A note on code in this plan

Security-critical code is given verbatim below and must be used as written: argv
construction, the environment allowlist, the symlink and identity checks, atomic replace,
and the CSRF/origin guard. Templates and routine wiring are specified by their required
behaviour and interfaces rather than line by line — follow the shape of the existing
`pr_dashboard/templates/` and the existing routes.

---

### Task 1: Store schema for invocations

**Files:** Modify `pr_dashboard/store.py`; Test `tests/unittest/test_pr_dashboard_store.py`

**Produces:** `ui_runs` table; `runs.dashboard_token` column;
`start_ui_run(conn, *, token, provider, repo_slug, pr_number, pr_url, command, log_path, started_at) -> None`;
`finish_ui_run(conn, *, token, status, exit_code, finished_at) -> None`;
`get_ui_run(conn, token) -> sqlite3.Row | None`;
`list_ui_runs(conn, limit=50) -> list[sqlite3.Row]`;
`run_for_token(conn, token) -> sqlite3.Row | None`.

Add to `migrate()` exactly the `ui_runs` DDL in spec section A, plus:

```sql
ALTER TABLE runs ADD COLUMN dashboard_token TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS runs_dashboard_token ON runs(dashboard_token);
```

`ALTER TABLE` is not idempotent — guard it by inspecting `PRAGMA table_info(runs)` rather
than catching an exception, so a real error is not swallowed. A `UNIQUE` index over a
nullable column permits many NULLs in SQLite, which is what we want: only dashboard-launched
runs carry a token.

**Tests:** migrate is idempotent on an already-migrated database **and preserves rows**
(write a row, migrate again, assert it survives — a DROP-and-recreate must fail this);
`start_ui_run` then `get_ui_run` round-trips every field; `finish_ui_run` sets status and
exit code; two rows with NULL `dashboard_token` can coexist but two with the same non-NULL
token cannot.

---

### Task 2: CSRF and origin guard

**Files:** Create `pr_dashboard/websec.py`; Modify `pr_dashboard/app.py`;
Test `tests/unittest/test_pr_dashboard_websec.py`

**Produces:** `csrf_token(request) -> str`; `require_safe_request(request, form_values) -> None`
(raises `HTTPException(403)`); `SAFE_HOSTS`.

This guards every state-changing route added in Tasks 5, 8 and 10. Build it first.

Session state: a signed cookie is overkill here; store a token in a server-side dict keyed
by a random session id cookie, created on first page load, `httponly=True`, `samesite="strict"`.

```python
def require_safe_request(request: Request, form_values: dict[str, str]) -> None:
    """Reject a state-changing request that a foreign page could have forged."""
    # Origin first: it is present on every cross-origin POST a browser makes, and its
    # absence on a state-changing request is itself suspicious, so absence is a rejection
    # rather than a pass. Referer is the fallback for same-origin posts that omit Origin.
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin or urlparse(origin).netloc not in SAFE_HOSTS:
        raise HTTPException(status_code=403, detail="cross-origin request refused")
    if request.headers.get("host", "").split(":")[0] not in {"127.0.0.1", "localhost"}:
        raise HTTPException(status_code=403, detail="unexpected host")
    expected = _session_token(request)
    supplied = form_values.get("csrf_token", "")
    # compare_digest, not ==: a plain comparison leaks token content through timing.
    if not expected or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="invalid csrf token")
```

`base.html` gains a hidden `csrf_token` input helper; every form in Tasks 5, 8 and 10
includes it.

**Tests, all of which must assert the side effect did not happen, not merely the status
code:** a foreign `Origin` is refused; a missing `Origin` is refused; a missing token is
refused; a wrong token is refused; a valid same-origin request with the right token passes.
Add one test asserting the existing S1+S2 `POST /repos` still works — this task touches a
shared surface and must not break it.

---

### Task 3: Run launcher

**Files:** Create `pr_dashboard/runner.py`; Test `tests/unittest/test_pr_dashboard_runner.py`

**Produces:** `ALLOWED_COMMANDS`, `MAX_CONCURRENT_RUNS = 2`, `RUN_TIMEOUT_SECONDS = 1800`,
`RunError`, `build_argv(repo, number, command, question=None) -> list[str]`,
`build_env() -> dict[str, str]`, `validate_target(registry_path, provider, slug, number) -> Repo`,
`launch(conn, *, repo, number, command, question=None) -> str` (returns the token).

Argv construction — use verbatim:

```python
ALLOWED_COMMANDS = ("review", "improve", "describe", "ask")

def build_argv(repo, number: int, command: str, question: Optional[str] = None) -> list[str]:
    """Build the child argv. Never a shell string, and never caller text in a flag position."""
    if command not in ALLOWED_COMMANDS:
        raise RunError(f"unsupported command {command!r}; expected one of {', '.join(ALLOWED_COMMANDS)}")
    # The URL is rebuilt from the validated registry entry, never taken from the request,
    # so even a validation gap cannot put attacker-controlled text on the command line.
    argv = ["uv", "run", "pr-agent", "--pr_url", pr_url_for(repo, number), command]
    if command == "ask":
        if not question:
            raise RunError("ask requires a question")
        # encode_user_text_arg, not a bare argv element: pr_agent/cli.py turns any argument
        # beginning "--" into a settings override, so an unencoded question of
        # --pr_questions.extra_instructions=... would silently reconfigure the run.
        # pr_agent/servers/azuredevops_server_webhook.py:137 encodes for the same reason.
        argv.append(encode_user_text_arg(question))
    elif question:
        raise RunError(f"{command} does not take free text")
    return argv
```

Environment allowlist — the child must not inherit `PR_AGENT_EXTRA_CONFIG_URL`, proxy, or
exporter variables. Carry only `PATH`, `HOME`, `LANG`, `LC_ALL`, `PYTHONPATH`, the provider
and model credential variables the run needs, and `PR_DASHBOARD_RUN_TOKEN`.

`validate_target` implements spec section A's URL rules: https only, exact provider host
(no suffix matching), no userinfo/query/fragment, canonical path, and `(provider, slug)`
present in the registry. It returns the registry `Repo`, and callers build the URL from it.

`launch` writes the `queued` row **before** `Popen`, spawns with `shell=False` and the
allowlisted env, records the pid, and enforces `MAX_CONCURRENT_RUNS`.

**Tests — no test may spawn a real process; stub `subprocess.Popen`:**
a command outside the whitelist is refused; a question on a non-`ask` command is refused;
an `ask` question of `--pr_questions.extra_instructions=x` reaches argv **encoded**, and a
test asserts the raw string is not present in any argv element; `build_env()` excludes
`PR_AGENT_EXTRA_CONFIG_URL`, `HTTP_PROXY` and `OTEL_EXPORTER_OTLP_ENDPOINT` when they are
set in the ambient environment; `Popen` is called with a list and `shell=False`
(assert on the call kwargs); a host of `github.com.evil.test` is refused; a URL with
userinfo is refused; an unregistered slug is refused; the cap refuses a third concurrent
run; a `queued` row exists even when `Popen` then raises.

---

### Task 4: Log redaction

**Files:** Create `pr_dashboard/redaction.py`; Test `tests/unittest/test_pr_dashboard_redaction.py`

**Produces:** `RedactionUnavailable`, `secret_values() -> list[str]`, `redact(text) -> str`.

Three passes per spec section A: exact configured secret values; their URL-encoded and
Base64 forms; and structural patterns — `Authorization: <scheme> <blob>`, `ghp_`/
`github_pat_`/`sk-` prefixes, and long high-entropy hex or Base64 runs.

Fail closed: if the secret inventory cannot be read, raise `RedactionUnavailable`; the
caller renders a message instead of the log. An empty secret set is not "nothing to
redact" — the structural passes still run.

**Tests:** a fake token in a log is absent from the output (assert the token's absence, not
that `***` is present — the latter passes on an empty render); its URL-encoded form is
absent; its Base64 form is absent; an `Authorization: Bearer <blob>` line is redacted with
no secret configured at all; `RedactionUnavailable` propagates when the inventory read
fails; ordinary log text is unchanged.

---

### Task 5: S3 routes and templates

**Files:** Modify `pr_dashboard/app.py`; Create `pr_dashboard/templates/runs.html`,
`run_detail.html`, `_run_rows.html`; Test `tests/unittest/test_pr_dashboard_runs.py`

Routes per spec section A. Every `POST` calls `require_safe_request` first.
`GET /runs/{token}` renders status, the **redacted** log tail, and exactly one of: the
accounting row; "accounting not enabled for this run"; "the run ended before accounting
started". HTMX polls only while status is non-terminal.

**Tests:** starting a run writes a `queued` row and returns the run page; a terminal run
does not emit a polling attribute; the log tail is redacted (seed a log with a fake token,
assert absence); each of the three accounting states renders its own text; cancel
terminates and records `cancelled`; the CSRF negatives from Task 2 applied to each POST,
asserting **no process was spawned**.

---

### Task 6: Config discovery

**Files:** Create `pr_dashboard/config_files.py`;
Test `tests/unittest/test_pr_dashboard_config_files.py`

**Produces:** `APPROVED_ROOTS`, `ConfigFile` (index, path, group, is_prompt),
`discover(repo_root) -> list[ConfigFile]`, `resolve(index) -> ConfigFile`,
`file_identity(path) -> tuple[int, int]`, `assert_safe_target(path) -> None`.

`discover` globs the three groups in spec section B and **excludes `.secrets.toml` and
`settings_prod/` entirely**.

Safety check — use verbatim:

```python
def assert_safe_target(path: Path) -> None:
    """Refuse anything but a regular file resolving beneath an approved root."""
    # lstat, not stat: stat follows the link and would happily report the *target* as a
    # regular file, which is exactly the case being rejected. A discovered
    # `.pr_agent.toml -> ~/.ssh/config` is a legitimate discovery result whose target must
    # never be written.
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ConfigFileError(f"{path} is not a regular file")
    resolved = path.resolve()
    if not any(resolved.is_relative_to(root) for root in APPROVED_ROOTS):
        raise ConfigFileError(f"{path} resolves outside the approved roots")
```

**Tests:** `.secrets.toml` is never discovered; `settings_prod/` is never discovered; a
symlink whose target is outside an approved root is refused by `assert_safe_target` **and
the target file is byte-unchanged afterwards**; a FIFO is refused; a genuine settings file
passes; `resolve` on an out-of-range index raises rather than returning a neighbour.

---

### Task 7: Safe write pipeline

**Files:** Create `pr_dashboard/config_writer.py`;
Test `tests/unittest/test_pr_dashboard_config_writer.py`

**Produces:** `PreviewToken`, `validate(text, is_prompt) -> None`,
`make_preview(config_file, submitted) -> tuple[str, PreviewToken]`,
`apply(token_value, submitted) -> Path` (returns the backup path),
`list_backups() -> list[dict]`, `restore(backup_id) -> None`.

`validate` parses with `tomlkit.parse`, and for a prompt file additionally compiles the
text as a Jinja2 template with `StrictUndefined` so an unclosed block is caught before a
run consumes it.

`PreviewToken` binds, server-side: canonical resolved path, file identity, original-content
hash, submitted-content hash, expiry, and single-use. `apply` re-checks every one and
re-runs `assert_safe_target` immediately before replacing.

Atomic write — use verbatim:

```python
def _atomic_write(path: Path, text: str) -> None:
    """Replace `path` atomically so a crash cannot leave a truncated config behind."""
    # Same directory, because os.replace is only atomic within a filesystem. A temp file in
    # /tmp could land on another device and degrade to a copy, reintroducing the torn write.
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
        # fsync the directory too: without it the rename itself can be lost on power failure
        # even though the file contents were flushed.
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
```

Backups are written `0600` and fsynced before the target is touched, with a recorded hash
`restore` verifies. Writes are serialised per canonical path with a lock.

**Tests:** invalid TOML is refused and the file is unchanged; a prompt with an unclosed
Jinja block is refused; a spent token is refused; an expired token is refused; a token
whose original-content hash no longer matches is refused; a token minted for one file
cannot apply to another; comments and section order survive a write (assert the exact
comment text is still present); the backup content equals the previous file; `restore`
refuses a backup whose hash does not verify; after `_atomic_write` no `.tmp` file remains
in the directory.

---

### Task 8: S4 routes and templates

**Files:** Modify `pr_dashboard/app.py`; Create `pr_dashboard/templates/config_list.html`,
`config_edit.html`, `config_backups.html`; Test `tests/unittest/test_pr_dashboard_config_routes.py`

Routes per spec section B, all POSTs guarded by `require_safe_request`. The edit page
labels prompt files as prompts and says `StrictUndefined` applies.

**Tests:** the listing never shows `.secrets.toml`; preview returns a diff and a token;
applying with that token writes and creates a backup; replaying the same token is refused;
a POST with a foreign `Origin` writes nothing (assert file bytes unchanged).

---

### Task 9: Findings index

**Files:** Create `pr_dashboard/findings_index.py`;
Test `tests/unittest/test_pr_dashboard_findings_index.py`

**Produces:** `MAX_INDEXED_PRS_PER_REPO = 20`, `REPO_DEADLINE_SECONDS`,
`TOTAL_DEADLINE_SECONDS`, `RepoIndexState`, `index_snapshot(conn, repos) -> dict`,
`refresh(conn, repos) -> None`, `filter_rows(rows, **filters) -> list`.

`index_snapshot` **never fetches** — it reads cache only and reports each repository as
fresh, stale, loading, or failed. `refresh` does the fetching, sequentially, under both
deadlines, single-flighted per repository so concurrent callers coalesce, with backoff on
failure.

**Tests:** `index_snapshot` performs no provider call at all (stub the provider with a
function that fails the test if called); a repository over its deadline is reported
`loading`, not waited on; two concurrent refreshes of one repository produce **one**
provider call; a failing repository is reported `failed` with its own message while the
others still return rows; the per-repo cap is honoured; each filter narrows correctly and
an unknown filter value returns empty rather than everything.

---

### Task 10: S5 route, template, and documentation

**Files:** Modify `pr_dashboard/app.py`, `pr_dashboard/templates/base.html`,
`docs/docs/tools/dashboard.md`; Create `pr_dashboard/templates/findings.html`;
Test `tests/unittest/test_pr_dashboard_findings_routes.py`

`GET /findings` renders the snapshot immediately with per-repository state and polls while
any repository is `loading`. Filters are query parameters. Nav gains Runs, Config, Findings.

Documentation must cover: enabling `record_runs` for run accounting; that runs execute as
subprocesses with a reduced environment, and that an exported variable will therefore not
reach them; that config edits write to the working tree only and never commit; where
backups live; and that the findings view covers the most recent N PRs per repository rather
than all of them.

**Tests:** the page renders with zero provider calls on a cold cache; a `loading`
repository shows as loading and the page polls; filters narrow the table; the "most recent
N" caveat is present in the rendered output.

---

## Final verification

After Task 10: full `PYTHONPATH=. uv run pytest -q`, `uv run ruff check`, and a manual
smoke of every route on a real `create_app` instance, asserting no route 500s and that
`POST` routes refuse a foreign `Origin`.
