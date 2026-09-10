# PR-Agent Dashboard S1 + S2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local single-user dashboard that lists every connected GitHub and Bitbucket repository, shows the reviews PR-Agent posted on their pull requests, and reports token and cost consumption per run.

**Architecture:** A new sibling package `pr_dashboard/` runs a FastAPI app serving Jinja2 templates with HTMX partials. Pull requests and review comments are read live from the provider APIs (cached briefly in SQLite); token and cost usage is persisted by a single opt-in hook that reads the `RunDetails` collector PR-Agent already maintains. Only two small edits land inside `pr_agent/`.

**Tech Stack:** Python ≥ 3.12, FastAPI, uvicorn, Jinja2, pydantic, stdlib `sqlite3` and `tomllib`, PyGithub, `requests` — all already declared in `pyproject.toml`. HTMX, Alpine, and Chart.js are vendored under `pr_dashboard/static/`, not installed via npm.

**Spec:** `docs/superpowers/specs/2026-09-10-pr-dashboard-s1-s2-design.md`

## Global Constraints

- **Blocker to clear before Task 1.** `pyproject.toml:141` pins `required-version = "==0.12.10"` and the installed `uv` is `0.10.9`, so every `uv run` in this plan currently fails with `Required uv version ==0.12.10 does not match the running version 0.10.9`. Upgrade uv (`uv self update`, or reinstall) before starting. Do **not** relax the pin in `pyproject.toml` to work around it — that file is shared with CI.
- Python ≥ 3.12 as declared in `pyproject.toml`. The system `python3` on this machine is 3.9 — always run through `uv run`, never bare `python3`. The project virtualenv interpreter is `.venv/bin/python` (3.12) if a one-off check is needed while uv is being fixed.
- Run tests as `PYTHONPATH=. uv run pytest <path> -q`. Pytest config lives in `pyproject.toml` with `asyncio_mode = "auto"` and `testpaths = ["tests/unittest"]`.
- Maximum line length 120 characters.
- Prefer double quotes for Python strings.
- Ruff rules `E`, `F`, `B`, `I` are enforced. Run `uv run ruff check --fix` on touched files only, and never add an entry to `lint.ignore`.
- Run `uv run pre-commit run --files <paths>` on touched files before each commit and review the automatic edits.
- **No new runtime dependencies.** If a task appears to need one, stop and report instead of adding it.
- Do not reformat, reorder, or re-sort any existing file. Edits inside `pr_agent/` are limited to exactly what a task names.
- TOML files under `pr_agent/settings/` keep their formatting, section order, and comments.
- New unit tests go in `tests/unittest/test_pr_dashboard_*.py` and follow the existing style: a `class TestX:` with one docstring line per test method (see `tests/unittest/test_fix_json_escape_char.py`).
- Costs are `Decimal` end to end and stored as `TEXT`. Never introduce `float` into a cost path.
- Never write, log, or template a provider token.

---

### Task 1: Usage store

**Files:**
- Create: `pr_dashboard/__init__.py`
- Create: `pr_dashboard/store.py`
- Test: `tests/unittest/test_pr_dashboard_store.py`

**Interfaces:**
- Consumes: `pr_agent.algo.run_details.RunDetails` (read-only; fields `model_used`, `fallback_used`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `num_ai_calls`, `known_cost_call_count`, `cost_status`, `total_cost_usd`, `model_costs_usd`, `duration_seconds`).
- Produces:
  - `DEFAULT_DB_PATH: pathlib.Path`
  - `connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection`
  - `migrate(conn: sqlite3.Connection) -> None`
  - `start_run(conn, *, provider: str, command: str, pr_url: str | None, repo_slug: str | None, pr_number: int | None, started_at: str) -> int`
  - `finish_run(conn, run_id: int, *, status: str, finished_at: str, details=None, error_text: str | None = None) -> None`
  - `get_run(conn, run_id: int) -> sqlite3.Row | None`
  - `run_cost(row: sqlite3.Row) -> Decimal | None`

- [ ] **Step 1: Write the failing test**

Create `tests/unittest/test_pr_dashboard_store.py`:

```python
from decimal import Decimal

from pr_agent.algo.run_details import RunDetails
from pr_dashboard import store


class TestStoreSchema:
    def test_connect_creates_tables(self):
        """A fresh connection has the runs, run_model_costs and provider_cache tables"""
        conn = store.connect(":memory:")
        names = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"runs", "run_model_costs", "provider_cache"} <= names

    def test_migrate_is_idempotent(self):
        """Running the migration twice leaves the schema unchanged and raises nothing"""
        conn = store.connect(":memory:")
        store.migrate(conn)
        store.migrate(conn)
        assert conn.execute("SELECT count(*) AS n FROM runs").fetchone()["n"] == 0


class TestRunLifecycle:
    def test_start_run_records_an_attempt(self):
        """start_run inserts a running row carrying the identity columns"""
        conn = store.connect(":memory:")
        run_id = store.start_run(
            conn,
            provider="github",
            command="review",
            pr_url="https://github.com/o/r/pull/7",
            repo_slug="o/r",
            pr_number=7,
            started_at="2026-09-10T10:00:00Z",
        )
        row = store.get_run(conn, run_id)
        assert row["status"] == "running"
        assert row["repo_slug"] == "o/r"
        assert row["pr_number"] == 7
        assert row["total_tokens"] is None

    def test_finish_run_without_details_keeps_usage_null(self):
        """A run that never reached a tool is completed with an error and no usage"""
        conn = store.connect(":memory:")
        run_id = store.start_run(
            conn, provider="github", command="review", pr_url=None,
            repo_slug=None, pr_number=None, started_at="2026-09-10T10:00:00Z",
        )
        store.finish_run(
            conn, run_id, status="failed", finished_at="2026-09-10T10:00:01Z",
            details=None, error_text="bad url",
        )
        row = store.get_run(conn, run_id)
        assert row["status"] == "failed"
        assert row["error_text"] == "bad url"
        assert row["total_tokens"] is None
        assert store.run_cost(row) is None

    def test_finish_run_round_trips_decimal_cost(self):
        """Cost survives storage exactly, with no float rounding"""
        conn = store.connect(":memory:")
        details = RunDetails()
        details.model_used = "gemini/gemini-3.5-flash"
        details.prompt_tokens = 1200
        details.completion_tokens = 300
        details.total_tokens = 1500
        details.num_ai_calls = 2
        details.known_cost_call_count = 2
        details.total_cost_usd = Decimal("0.0000123456789")
        details.model_costs_usd = {"gemini/gemini-3.5-flash": Decimal("0.0000123456789")}

        run_id = store.start_run(
            conn, provider="github", command="review", pr_url="https://github.com/o/r/pull/7",
            repo_slug="o/r", pr_number=7, started_at="2026-09-10T10:00:00Z",
        )
        store.finish_run(conn, run_id, status="ok", finished_at="2026-09-10T10:00:09Z", details=details)

        row = store.get_run(conn, run_id)
        assert row["status"] == "ok"
        assert row["total_tokens"] == 1500
        assert row["cost_status"] == "complete"
        assert store.run_cost(row) == Decimal("0.0000123456789")
        costs = conn.execute("SELECT model, cost_usd FROM run_model_costs WHERE run_id = ?", (run_id,)).fetchall()
        assert [(r["model"], r["cost_usd"]) for r in costs] == [
            ("gemini/gemini-3.5-flash", "0.0000123456789")
        ]

    def test_unpriced_run_stores_no_cost(self):
        """A run litellm could not price stores NULL, never a misleading zero"""
        conn = store.connect(":memory:")
        details = RunDetails()
        details.model_used = "ollama/llama3"
        details.total_tokens = 900
        details.num_ai_calls = 1
        run_id = store.start_run(
            conn, provider="github", command="improve", pr_url=None,
            repo_slug="o/r", pr_number=7, started_at="2026-09-10T10:00:00Z",
        )
        store.finish_run(conn, run_id, status="ok", finished_at="2026-09-10T10:00:05Z", details=details)
        row = store.get_run(conn, run_id)
        assert row["cost_status"] == "unavailable"
        assert row["total_cost_usd"] is None
        assert store.run_cost(row) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_store.py -q`

Expected: FAIL — `ModuleNotFoundError: No module named 'pr_dashboard'`

- [ ] **Step 3: Create the package marker**

Create `pr_dashboard/__init__.py`:

```python
"""Local dashboard for PR-Agent: repository browsing, review history, and usage accounting."""
```

- [ ] **Step 4: Implement the store**

Create `pr_dashboard/store.py`:

```python
"""SQLite-backed usage store for the PR-Agent dashboard.

Costs are stored as TEXT because ``RunDetails`` accumulates them as ``Decimal`` to keep
pricing free of float math; a REAL column would reintroduce exactly that. A NULL cost
means "could not be priced", which is different from free — see ``_as_decimal_cost`` in
``pr_agent/algo/run_details.py``.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path.home() / ".pr_dashboard" / "usage.db"
LOCK_TIMEOUT_SECONDS = 5.0

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        provider TEXT NOT NULL,
        repo_slug TEXT,
        pr_number INTEGER,
        pr_url TEXT,
        command TEXT NOT NULL,
        model_used TEXT,
        fallback_used INTEGER,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        total_tokens INTEGER,
        num_ai_calls INTEGER,
        known_cost_call_count INTEGER,
        cost_status TEXT,
        total_cost_usd TEXT,
        duration_seconds REAL,
        error_text TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_model_costs (
        run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        model TEXT NOT NULL,
        cost_usd TEXT NOT NULL,
        PRIMARY KEY (run_id, model)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_cache (
        key TEXT PRIMARY KEY,
        fetched_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS runs_started_at ON runs (started_at)",
    "CREATE INDEX IF NOT EXISTS runs_repo ON runs (provider, repo_slug)",
)

_FINISH_WITH_USAGE = (
    "UPDATE runs SET status = ?, finished_at = ?, error_text = ?, model_used = ?, "
    "fallback_used = ?, prompt_tokens = ?, completion_tokens = ?, total_tokens = ?, "
    "num_ai_calls = ?, known_cost_call_count = ?, cost_status = ?, total_cost_usd = ?, "
    "duration_seconds = ? WHERE id = ?"
)


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the store, creating the file and schema when absent."""
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # timeout gives the bounded retry the spec asks for on "database is locked": sqlite
    # waits up to this long for the writer lock before raising OperationalError.
    conn = sqlite3.connect(str(db_path), timeout=LOCK_TIMEOUT_SECONDS, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Apply the schema. Every statement is IF NOT EXISTS, so this is idempotent."""
    for statement in _SCHEMA:
        conn.execute(statement)


def start_run(conn: sqlite3.Connection, *, provider: str, command: str, pr_url: Optional[str],
              repo_slug: Optional[str], pr_number: Optional[int], started_at: str) -> int:
    """Record an attempt before the command runs, so failures are not invisible."""
    cursor = conn.execute(
        "INSERT INTO runs (started_at, status, provider, repo_slug, pr_number, pr_url, command) "
        "VALUES (?, 'running', ?, ?, ?, ?, ?)",
        (started_at, provider, repo_slug, pr_number, pr_url, command),
    )
    return int(cursor.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, *, status: str, finished_at: str,
               details=None, error_text: Optional[str] = None) -> None:
    """Complete a run row, attaching usage when a RunDetails collector was installed."""
    if details is None:
        conn.execute(
            "UPDATE runs SET status = ?, finished_at = ?, error_text = ? WHERE id = ?",
            (status, finished_at, error_text, run_id),
        )
        return

    conn.execute(
        _FINISH_WITH_USAGE,
        (
            status,
            finished_at,
            error_text,
            details.model_used,
            int(bool(details.fallback_used)),
            details.prompt_tokens,
            details.completion_tokens,
            details.total_tokens,
            details.num_ai_calls,
            details.known_cost_call_count,
            details.cost_status,
            str(details.total_cost_usd) if details.known_cost_call_count else None,
            details.duration_seconds,
            run_id,
        ),
    )
    for model, cost in details.model_costs_usd.items():
        conn.execute(
            "INSERT OR REPLACE INTO run_model_costs (run_id, model, cost_usd) VALUES (?, ?, ?)",
            (run_id, model, str(cost)),
        )


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[sqlite3.Row]:
    """Return one run row, or None when the id is unknown."""
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def run_cost(row: sqlite3.Row) -> Optional[Decimal]:
    """Return a run's cost as Decimal, or None when it was never priced."""
    raw = row["total_cost_usd"]
    if raw is None:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_store.py -q`

Expected: PASS, 6 tests

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check --fix pr_dashboard/store.py pr_dashboard/__init__.py tests/unittest/test_pr_dashboard_store.py
uv run pre-commit run --files pr_dashboard/store.py pr_dashboard/__init__.py tests/unittest/test_pr_dashboard_store.py
git add pr_dashboard/__init__.py pr_dashboard/store.py tests/unittest/test_pr_dashboard_store.py
git commit -m "feat(dashboard): add SQLite usage store with Decimal-exact cost columns"
```

---

### Task 2: Repository registry

**Files:**
- Create: `pr_dashboard/registry.py`
- Test: `tests/unittest/test_pr_dashboard_registry.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `SUPPORTED_PROVIDERS: tuple[str, ...]` — `("github", "bitbucket")`
  - `SLUG_PATTERN: re.Pattern[str]`
  - `class Repo` — frozen dataclass with `provider: str`, `slug: str`, and property `key -> str` returning `f"{provider}:{slug}"`
  - `DEFAULT_REGISTRY_PATH: pathlib.Path`
  - `load(path=DEFAULT_REGISTRY_PATH) -> list[Repo]`
  - `save(repos: list[Repo], path=DEFAULT_REGISTRY_PATH) -> None`
  - `add(repo: Repo, path=DEFAULT_REGISTRY_PATH) -> list[Repo]`
  - `remove(provider: str, slug: str, path=DEFAULT_REGISTRY_PATH) -> list[Repo]`
  - `class RegistryError(ValueError)`

- [ ] **Step 1: Write the failing test**

Create `tests/unittest/test_pr_dashboard_registry.py`:

```python
import pytest

from pr_dashboard import registry


class TestRepoValidation:
    def test_rejects_unknown_provider(self):
        """Only github and bitbucket are accepted, and the message names the value"""
        with pytest.raises(registry.RegistryError) as excinfo:
            registry.Repo(provider="gitlab", slug="o/r").validate()
        assert "gitlab" in str(excinfo.value)

    def test_rejects_slug_without_owner(self):
        """A slug must be owner/name"""
        with pytest.raises(registry.RegistryError):
            registry.Repo(provider="github", slug="block_rush").validate()

    def test_rejects_slug_with_quotes(self):
        """Characters that would need TOML escaping are refused outright"""
        with pytest.raises(registry.RegistryError):
            registry.Repo(provider="github", slug='o/r"evil').validate()

    def test_accepts_dots_and_dashes(self):
        """Real-world repository names with dots and dashes are valid"""
        registry.Repo(provider="bitbucket", slug="my-team/some.repo").validate()


class TestRegistryFile:
    def test_load_missing_file_returns_empty(self, tmp_path):
        """A first run has no registry file and that is not an error"""
        assert registry.load(tmp_path / "absent.toml") == []

    def test_round_trip(self, tmp_path):
        """Saved repositories load back identically and in order"""
        path = tmp_path / "pr_dashboard.toml"
        repos = [
            registry.Repo(provider="github", slug="samer2373/block_rush"),
            registry.Repo(provider="bitbucket", slug="team/service"),
        ]
        registry.save(repos, path)
        assert registry.load(path) == repos

    def test_add_rejects_duplicate(self, tmp_path):
        """The same provider and slug cannot be registered twice"""
        path = tmp_path / "pr_dashboard.toml"
        repo = registry.Repo(provider="github", slug="o/r")
        registry.add(repo, path)
        with pytest.raises(registry.RegistryError):
            registry.add(repo, path)

    def test_remove_unknown_is_an_error(self, tmp_path):
        """Removing something absent reports it instead of silently succeeding"""
        path = tmp_path / "pr_dashboard.toml"
        registry.add(registry.Repo(provider="github", slug="o/r"), path)
        with pytest.raises(registry.RegistryError):
            registry.remove("github", "other/repo", path)

    def test_remove_leaves_the_rest(self, tmp_path):
        """Removing one entry keeps the others"""
        path = tmp_path / "pr_dashboard.toml"
        registry.add(registry.Repo(provider="github", slug="o/one"), path)
        registry.add(registry.Repo(provider="github", slug="o/two"), path)
        remaining = registry.remove("github", "o/one", path)
        assert [r.slug for r in remaining] == ["o/two"]

    def test_load_skips_malformed_entries(self, tmp_path):
        """A hand-edited file with a bad entry loads the good ones and drops the bad"""
        path = tmp_path / "pr_dashboard.toml"
        path.write_text(
            '[[repo]]\nprovider = "github"\nslug = "o/good"\n\n'
            '[[repo]]\nprovider = "gitlab"\nslug = "o/bad"\n',
            encoding="utf-8",
        )
        assert [r.slug for r in registry.load(path)] == ["o/good"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_registry.py -q`

Expected: FAIL — `ImportError: cannot import name 'registry' from 'pr_dashboard'`

- [ ] **Step 3: Implement the registry**

Create `pr_dashboard/registry.py`:

```python
"""The list of repositories the dashboard shows.

The file is entirely machine-owned, so it is written by regenerating it rather than by
round-tripping with tomlkit. That is only safe because every value is validated first:
the provider comes from a fixed set and the slug cannot contain a character that would
need TOML escaping. Credentials never appear here; they stay in .secrets.toml and the
environment, read through pr_agent's get_settings().
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_PROVIDERS = ("github", "bitbucket")
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
DEFAULT_REGISTRY_PATH = Path.home() / ".pr_dashboard" / "pr_dashboard.toml"


class RegistryError(ValueError):
    """Raised for an invalid, duplicate, or unknown registry entry."""


@dataclass(frozen=True)
class Repo:
    provider: str
    slug: str

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.slug}"

    def validate(self) -> "Repo":
        """Return self when valid, else raise RegistryError naming the offending value."""
        if self.provider not in SUPPORTED_PROVIDERS:
            raise RegistryError(
                f"unsupported provider {self.provider!r}; expected one of {', '.join(SUPPORTED_PROVIDERS)}")
        if not SLUG_PATTERN.match(self.slug):
            raise RegistryError(f"invalid repository slug {self.slug!r}; expected owner/name")
        return self


def load(path: Path | str = DEFAULT_REGISTRY_PATH) -> list[Repo]:
    """Read the registry, dropping entries a human edit made invalid."""
    path = Path(path)
    if not path.exists():
        return []
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    repos: list[Repo] = []
    for entry in data.get("repo", []):
        if not isinstance(entry, dict):
            continue
        candidate = Repo(provider=str(entry.get("provider", "")), slug=str(entry.get("slug", "")))
        try:
            repos.append(candidate.validate())
        except RegistryError:
            continue
    return repos


def save(repos: list[Repo], path: Path | str = DEFAULT_REGISTRY_PATH) -> None:
    """Rewrite the whole registry file from validated entries."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = ["# Managed by pr-dashboard. Credentials are NOT stored here.\n"]
    for repo in repos:
        repo.validate()
        blocks.append(f'[[repo]]\nprovider = "{repo.provider}"\nslug = "{repo.slug}"\n')
    path.write_text("\n".join(blocks), encoding="utf-8")


def add(repo: Repo, path: Path | str = DEFAULT_REGISTRY_PATH) -> list[Repo]:
    """Append a repository, refusing a duplicate."""
    repo.validate()
    repos = load(path)
    if any(existing.key == repo.key for existing in repos):
        raise RegistryError(f"{repo.slug} is already registered for {repo.provider}")
    repos.append(repo)
    save(repos, path)
    return repos


def remove(provider: str, slug: str, path: Path | str = DEFAULT_REGISTRY_PATH) -> list[Repo]:
    """Drop a repository, reporting when it was not registered."""
    repos = load(path)
    remaining = [repo for repo in repos if repo.key != f"{provider}:{slug}"]
    if len(remaining) == len(repos):
        raise RegistryError(f"{slug} is not registered for {provider}")
    save(remaining, path)
    return remaining
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_registry.py -q`

Expected: PASS, 10 tests

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check --fix pr_dashboard/registry.py tests/unittest/test_pr_dashboard_registry.py
uv run pre-commit run --files pr_dashboard/registry.py tests/unittest/test_pr_dashboard_registry.py
git add pr_dashboard/registry.py tests/unittest/test_pr_dashboard_registry.py
git commit -m "feat(dashboard): add validated repository registry backed by a TOML file"
```

---

### Task 3: Usage recorder and the two `pr_agent/` edits

**Files:**
- Create: `pr_dashboard/recorder.py`
- Modify: `pr_agent/agent/pr_agent.py:279` (add one import near the existing imports, and one context manager to the existing `with` statement)
- Modify: `pr_agent/settings/configuration.toml` (append a new `[pr_dashboard]` section)
- Test: `tests/unittest/test_pr_dashboard_recorder.py`

**Interfaces:**
- Consumes: `pr_dashboard.store` (`connect`, `start_run`, `finish_run`), `pr_agent.algo.run_details.get_run_details`, `pr_agent.config_loader.get_settings`.
- Produces:
  - `record_run(*, pr_url: str | None, command: str) -> ContextManager[None]`
  - `parse_pr_url(pr_url: str | None) -> tuple[str | None, int | None]` returning `(repo_slug, pr_number)`
  - `recording_enabled() -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/unittest/test_pr_dashboard_recorder.py`:

```python
import asyncio
import sqlite3

import pytest

from pr_agent.algo.run_details import init_run_details, record_ai_call
from pr_dashboard import recorder, store


class _Usage:
    prompt_tokens = 800
    completion_tokens = 200
    total_tokens = 1000


@pytest.fixture
def conn(tmp_path, monkeypatch):
    connection = store.connect(tmp_path / "usage.db")
    monkeypatch.setattr(recorder, "_open_store", lambda: connection)
    monkeypatch.setattr(recorder, "recording_enabled", lambda: True)
    return connection


class TestParsePrUrl:
    def test_github_url(self):
        """A GitHub pull request URL yields the slug and number"""
        assert recorder.parse_pr_url("https://github.com/samer2373/block_rush/pull/1") == (
            "samer2373/block_rush", 1)

    def test_bitbucket_url(self):
        """A Bitbucket pull request URL yields the slug and number"""
        assert recorder.parse_pr_url("https://bitbucket.org/team/svc/pull-requests/42") == (
            "team/svc", 42)

    def test_unparsable_url_is_not_fatal(self):
        """An unrecognised URL yields no identity rather than raising"""
        assert recorder.parse_pr_url("not a url") == (None, None)

    def test_none_url(self):
        """A missing URL yields no identity"""
        assert recorder.parse_pr_url(None) == (None, None)


class TestRecordRun:
    def test_records_usage_set_inside_the_body(self, conn):
        """RunDetails installed inside the with-body is visible when the block exits"""
        async def run_tool():
            init_run_details()
            record_ai_call(_Usage(), model="gemini/gemini-3.5-flash", cost_usd="0.0004")

        async def main():
            with recorder.record_run(pr_url="https://github.com/o/r/pull/7", command="review"):
                await run_tool()

        asyncio.run(main())

        row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert row["status"] == "ok"
        assert row["command"] == "review"
        assert row["repo_slug"] == "o/r"
        assert row["pr_number"] == 7
        assert row["total_tokens"] == 1000
        assert row["model_used"] == "gemini/gemini-3.5-flash"
        assert store.run_cost(row) is not None

    def test_records_an_attempt_when_no_collector_was_installed(self, conn):
        """A command that never calls init_run_details still leaves an auditable row"""
        with recorder.record_run(pr_url="https://github.com/o/r/pull/7", command="ask"):
            pass
        row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert row["status"] == "ok"
        assert row["total_tokens"] is None
        assert row["cost_status"] is None

    def test_records_failure_and_reraises(self, conn):
        """An exception is recorded and then propagates unchanged"""
        with pytest.raises(RuntimeError, match="boom"):
            with recorder.record_run(pr_url="https://github.com/o/r/pull/7", command="review"):
                raise RuntimeError("boom")
        row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert row["status"] == "failed"
        assert "boom" in row["error_text"]

    def test_disabled_recording_writes_nothing(self, tmp_path, monkeypatch):
        """With pr_dashboard.record_runs off, no database is opened at all"""
        monkeypatch.setattr(recorder, "recording_enabled", lambda: False)
        def explode():
            raise AssertionError("the store must not be opened when recording is disabled")
        monkeypatch.setattr(recorder, "_open_store", explode)
        with recorder.record_run(pr_url="https://github.com/o/r/pull/7", command="review"):
            pass

    def test_store_failure_never_breaks_the_run(self, monkeypatch):
        """A recorder error is swallowed, because a dashboard write must not fail a review"""
        monkeypatch.setattr(recorder, "recording_enabled", lambda: True)

        def explode():
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(recorder, "_open_store", explode)
        with recorder.record_run(pr_url="https://github.com/o/r/pull/7", command="review"):
            pass


class TestDispatchWiring:
    def test_pr_agent_wraps_dispatch_with_record_run(self):
        """pr_agent's dispatch is wrapped, and the tool is still awaited directly.

        The recorder relies on ContextVar propagation through a direct await. An upstream
        refactor to asyncio.gather or create_task around the tool would silently zero all
        usage, so assert the shape here rather than discovering it in the dashboard.
        """
        import inspect

        from pr_agent.agent.pr_agent import PRAgent

        source = inspect.getsource(PRAgent._handle_request)
        assert "record_run(" in source
        dispatch_line = next(line for line in source.splitlines() if "command2class[action](" in line)
        assert dispatch_line.strip().startswith("await ")
        # Scoped to _handle_request on purpose: the module legitimately uses
        # asyncio.to_thread elsewhere (flush_telemetry), so a whole-file assertion would be
        # brittle and would tempt a future reader to weaken it.
        assert "gather" not in source
        assert "create_task" not in source
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_recorder.py -q`

Expected: FAIL — `ImportError: cannot import name 'recorder' from 'pr_dashboard'`

- [ ] **Step 3: Implement the recorder**

Create `pr_dashboard/recorder.py`:

```python
"""Persist one row per PR-Agent command run.

Two deliberate asymmetries live here.

First, a row is written on entry, not only on completion: a malformed URL, an auth
failure, or a rate-limit error means no tool ever installs a RunDetails collector, and a
completion-only table would silently under-report exactly the runs worth seeing.

Second, every failure in this module is logged and swallowed. A dashboard write must
never break a review. That is the opposite of the rule everywhere else in the dashboard,
so it is stated here rather than left implicit.
"""
from __future__ import annotations

import contextlib
import re
from datetime import datetime, timezone
from typing import Optional

from pr_agent.algo.run_details import get_run_details
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_dashboard import store

_GITHUB_PR = re.compile(r"https?://[^/]+/([^/]+/[^/]+)/pull/(\d+)")
_BITBUCKET_PR = re.compile(r"https?://[^/]+/([^/]+/[^/]+)/pull-requests/(\d+)")
_GITLAB_MR = re.compile(r"https?://[^/]+/(.+?)/-/merge_requests/(\d+)")


def recording_enabled() -> bool:
    """Recording is opt-in: webhook and serverless deployments must not create a database."""
    return bool(get_settings().get("pr_dashboard.record_runs", False))


def parse_pr_url(pr_url: Optional[str]) -> tuple[Optional[str], Optional[int]]:
    """Return (repo_slug, pr_number) from a pull-request URL, or (None, None)."""
    if not pr_url:
        return None, None
    for pattern in (_GITHUB_PR, _BITBUCKET_PR, _GITLAB_MR):
        match = pattern.search(pr_url)
        if match:
            return match.group(1), int(match.group(2))
    return None, None


def _open_store():
    """Indirection so tests can substitute an in-tmpdir connection."""
    return store.connect()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextlib.contextmanager
def record_run(*, pr_url: Optional[str], command: str):
    """Record one command run, including runs that fail before reaching a tool."""
    if not recording_enabled():
        yield
        return

    conn = None
    run_id = None
    try:
        conn = _open_store()
        repo_slug, pr_number = parse_pr_url(pr_url)
        run_id = store.start_run(
            conn,
            provider=str(get_settings().get("config.git_provider", "unknown")),
            command=command,
            pr_url=pr_url,
            repo_slug=repo_slug,
            pr_number=pr_number,
            started_at=_now(),
        )
    except Exception as exc:  # noqa: BLE001 - a dashboard write must never fail a review
        get_logger().warning(f"pr_dashboard: could not start a usage row: {exc}")

    try:
        yield
    except Exception as exc:
        _finish(conn, run_id, status="failed", error_text=f"{type(exc).__name__}: {exc}")
        raise
    else:
        _finish(conn, run_id, status="ok", error_text=None)


def _finish(conn, run_id, *, status: str, error_text: Optional[str]) -> None:
    if conn is None or run_id is None:
        return
    try:
        store.finish_run(
            conn, run_id, status=status, finished_at=_now(),
            details=get_run_details(), error_text=error_text,
        )
    except Exception as exc:  # noqa: BLE001 - see module docstring
        get_logger().warning(f"pr_dashboard: could not finish usage row {run_id}: {exc}")
```

- [ ] **Step 4: Wire the recorder into the dispatch**

In `pr_agent/agent/pr_agent.py`, add the import beside the existing `pr_agent` imports:

```python
from pr_dashboard.recorder import record_run
```

Then change the single `with` statement at line 279 from:

```python
        with get_logger().contextualize(command=action, pr_url=pr_url):
```

to:

```python
        with get_logger().contextualize(command=action, pr_url=pr_url), record_run(pr_url=pr_url, command=action):
```

Do not re-indent the body. Verify the new line is within 120 characters:

```bash
awk 'length > 120 {print FILENAME":"NR": "length}' pr_agent/agent/pr_agent.py
```

Expected: no output.

- [ ] **Step 5: Add the configuration default**

Append to `pr_agent/settings/configuration.toml`, preserving the file's existing section order and comment style:

```toml
[pr_dashboard]
# Record one row per command run (model, tokens, cost, duration) into the local dashboard
# database at ~/.pr_dashboard/usage.db. Off by default: webhook and serverless deployments
# must not start writing a database just because the dashboard package is installed.
record_runs = false
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_recorder.py -q`

Expected: PASS, 10 tests

- [ ] **Step 7: Verify nothing else regressed**

The dispatch path is shared by every tool, so run the agent and settings suites before committing:

```bash
PYTHONPATH=. uv run pytest tests/unittest -q -k "pr_agent or settings or configuration"
```

Expected: PASS with no new failures.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check --fix pr_dashboard/recorder.py pr_agent/agent/pr_agent.py tests/unittest/test_pr_dashboard_recorder.py
uv run pre-commit run --files pr_dashboard/recorder.py pr_agent/agent/pr_agent.py pr_agent/settings/configuration.toml tests/unittest/test_pr_dashboard_recorder.py
git add pr_dashboard/recorder.py pr_agent/agent/pr_agent.py pr_agent/settings/configuration.toml tests/unittest/test_pr_dashboard_recorder.py
git commit -m "feat(dashboard): record per-run token and cost usage behind an opt-in flag"
```

---

### Task 4: Review comment identification and findings parsing

**Files:**
- Create: `pr_dashboard/comments.py`
- Create: `tests/unittest/fixtures/pr_dashboard/review_details.md`
- Create: `tests/unittest/fixtures/pr_dashboard/review_expanded.md`
- Create: `tests/unittest/fixtures/pr_dashboard/suggestions_summary.md`
- Test: `tests/unittest/test_pr_dashboard_comments.py`

**Interfaces:**
- Consumes: `pr_agent.algo.utils` (`PRReviewIdentity`, `PRCodeSuggestionsIdentity`, `_ALL_COMMENT_IDENTITIES`, `PRReviewHeader`, `PRCodeSuggestionsHeader`).
- Produces:
  - `class CommentKind(str, Enum)` with members `REVIEW`, `INCREMENTAL_REVIEW`, `SUGGESTIONS`, `OTHER`
  - `classify(body: str) -> CommentKind`
  - `is_pr_agent_comment(body: str) -> bool`
  - `class Finding` — frozen dataclass with `title: str`, `relevant_file: str | None`, `line_range: tuple[int, int] | None`
  - `parse_findings(body: str) -> list[Finding]`

**Fixture capture note:** the three fixture files are real comment bodies from
`samer2373/block_rush` pull request 1. Capture them with:

```bash
uv run python - <<'PY'
import json, pathlib, urllib.request, os
url = "https://api.github.com/repos/samer2373/block_rush/issues/1/comments"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {os.environ['TOKEN_GITHUB']}"})
comments = json.load(urllib.request.urlopen(req))
out = pathlib.Path("tests/unittest/fixtures/pr_dashboard")
out.mkdir(parents=True, exist_ok=True)
for c in comments:
    print(c["id"], c["body"][:80].replace("\n", " "))
PY
```

Save one review comment produced with `findings_layout = "details"`, one with
`findings_layout = "expanded"`, and one `/improve` summary. Strip nothing except any
absolute URL containing a token. If the token is unavailable, hand-write fixtures that
reproduce the exact marker and heading structure from
`pr_agent/algo/utils.py::render_focus_area_issue` and record in the commit message that
they are synthetic.

- [ ] **Step 1: Write the failing test**

Create `tests/unittest/test_pr_dashboard_comments.py`:

```python
from pathlib import Path

from pr_agent.algo.utils import (
    _ALL_COMMENT_IDENTITIES,
    PRCodeSuggestionsIdentity,
    PRReviewIdentity,
)
from pr_dashboard import comments

FIXTURES = Path("tests/unittest/fixtures/pr_dashboard")


class TestUpstreamContract:
    def test_identity_markers_still_exist(self):
        """Fail loudly if upstream renames the private identity tuple the dashboard imports"""
        assert PRReviewIdentity.REGULAR.value == "<!-- pr-agent:review:full -->"
        assert PRReviewIdentity.INCREMENTAL.value == "<!-- pr-agent:review:incremental -->"
        assert PRCodeSuggestionsIdentity.SUMMARY.value == "<!-- pr-agent:improve:summary -->"
        assert set(comments.IDENTITY_MARKERS) == set(_ALL_COMMENT_IDENTITIES)


class TestClassify:
    def test_full_review(self):
        """A full review is recognised from its identity marker"""
        body = f"{PRReviewIdentity.REGULAR.value}\n## Anything At All\n"
        assert comments.classify(body) is comments.CommentKind.REVIEW

    def test_incremental_review(self):
        """An incremental review is distinguished from a full one"""
        body = f"{PRReviewIdentity.INCREMENTAL.value}\n## x\n"
        assert comments.classify(body) is comments.CommentKind.INCREMENTAL_REVIEW

    def test_suggestions(self):
        """An improve summary is recognised"""
        body = f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n## x\n"
        assert comments.classify(body) is comments.CommentKind.SUGGESTIONS

    def test_renamed_heading_is_still_recognised(self):
        """A repo that overrode pr_reviewer.review_heading is still matched"""
        body = f"{PRReviewIdentity.REGULAR.value}\n## Our Custom Review Title\n"
        assert comments.classify(body) is comments.CommentKind.REVIEW

    def test_human_comment_is_not_ours(self):
        """An ordinary human comment is not attributed to PR-Agent"""
        assert comments.classify("looks good to me") is comments.CommentKind.OTHER
        assert comments.is_pr_agent_comment("looks good to me") is False

    def test_legacy_comment_without_marker_falls_back_to_heading(self):
        """Comments posted before identity markers existed are matched on the default heading"""
        assert comments.classify("## PR Reviewer Guide\n\nsome review") is comments.CommentKind.REVIEW


class TestParseFindings:
    def test_details_layout(self):
        """Findings are extracted from the collapsed details layout"""
        findings = comments.parse_findings((FIXTURES / "review_details.md").read_text(encoding="utf-8"))
        assert findings
        assert all(f.title for f in findings)

    def test_expanded_layout_exposes_file_and_lines(self):
        """The expanded layout yields the file path and line range as text, not only links"""
        findings = comments.parse_findings((FIXTURES / "review_expanded.md").read_text(encoding="utf-8"))
        assert findings
        assert any(f.relevant_file and f.line_range for f in findings)

    def test_no_findings_returns_empty(self):
        """A review with no findings yields an empty list, not an error"""
        body = f"{PRReviewIdentity.REGULAR.value}\n## PR Reviewer Guide\n\nNo key issues to review\n"
        assert comments.parse_findings(body) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_comments.py -q`

Expected: FAIL — `ImportError: cannot import name 'comments' from 'pr_dashboard'`

- [ ] **Step 3: Capture the fixtures**

Follow the fixture capture note above and write the three files under
`tests/unittest/fixtures/pr_dashboard/`. Confirm each contains its identity marker:

```bash
grep -c "pr-agent:" tests/unittest/fixtures/pr_dashboard/*.md
```

Expected: at least 1 for each file.

- [ ] **Step 4: Implement the parser**

Create `pr_dashboard/comments.py`:

```python
"""Recognise and parse the comments PR-Agent posts.

Identification uses the hidden identity markers PR-Agent already embeds in every comment
it publishes, imported from pr_agent rather than re-declared. Neither of the two obvious
alternatives is reliable: comment author fails because a self-hosted fork often posts
under a human token, and visible headings fail because a repository can override them
through pr_reviewer.review_heading. Heading matching survives only as a fallback for
comments posted before the markers existed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from pr_agent.algo.utils import (
    _ALL_COMMENT_IDENTITIES,
    PRCodeSuggestionsHeader,
    PRCodeSuggestionsIdentity,
    PRReviewHeader,
    PRReviewIdentity,
)

IDENTITY_MARKERS = tuple(_ALL_COMMENT_IDENTITIES)

# Fallback only, for comments published before identity markers existed. A repository that
# overrode its heading and has no marker cannot be recognised, which is accepted.
_LEGACY_HEADINGS = {
    PRReviewHeader.REGULAR.value: "REVIEW",
    PRReviewHeader.INCREMENTAL.value: "INCREMENTAL_REVIEW",
    PRCodeSuggestionsHeader.SUMMARY.value: "SUGGESTIONS",
}

_FILE_AND_LINES = re.compile(r"`?(?P<file>[\w./\-]+\.\w+)`?[^\n]*?\[?(?P<start>\d+)\s*[-–]\s*(?P<end>\d+)\]?")
_BULLET_TITLE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(?:\*\*)?(?P<title>[^*\n][^\n]*?)(?:\*\*)?\s*$")


class CommentKind(str, Enum):
    REVIEW = "review"
    INCREMENTAL_REVIEW = "incremental_review"
    SUGGESTIONS = "suggestions"
    OTHER = "other"


@dataclass(frozen=True)
class Finding:
    title: str
    relevant_file: Optional[str] = None
    line_range: Optional[tuple[int, int]] = None


def classify(body: str) -> CommentKind:
    """Return which PR-Agent output a comment body is, or OTHER when it is not ours."""
    if not body:
        return CommentKind.OTHER
    if PRReviewIdentity.INCREMENTAL.value in body:
        return CommentKind.INCREMENTAL_REVIEW
    if PRReviewIdentity.REGULAR.value in body:
        return CommentKind.REVIEW
    if any(marker in body for marker in (
        PRCodeSuggestionsIdentity.SUMMARY.value,
        PRCodeSuggestionsIdentity.NO_SUGGESTIONS.value,
        PRCodeSuggestionsIdentity.UNANCHORED.value,
    )):
        return CommentKind.SUGGESTIONS
    for heading, kind in _LEGACY_HEADINGS.items():
        if heading in body:
            return CommentKind[kind]
    return CommentKind.OTHER


def is_pr_agent_comment(body: str) -> bool:
    """True when the comment was published by PR-Agent."""
    return classify(body) is not CommentKind.OTHER


def parse_findings(body: str) -> list[Finding]:
    """Extract findings from a review comment, tolerating either findings_layout."""
    findings: list[Finding] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("<!--"):
            continue
        title_match = _BULLET_TITLE.match(raw_line)
        if not title_match:
            continue
        title = title_match.group("title").strip()
        if not title or title.lower().startswith("no key issues"):
            continue
        location = _FILE_AND_LINES.search(raw_line)
        if location:
            findings.append(Finding(
                title=title,
                relevant_file=location.group("file"),
                line_range=(int(location.group("start")), int(location.group("end"))),
            ))
        else:
            findings.append(Finding(title=title))
    return findings
```

- [ ] **Step 5: Run the test and adjust the regexes against the real fixtures**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_comments.py -q`

Expected: PASS, 10 tests. The two regexes are written against the layouts produced by
`render_focus_area_issue` in `pr_agent/algo/utils.py`; if a fixture assertion fails, read
that function and the failing fixture and adjust `_FILE_AND_LINES` or `_BULLET_TITLE` —
do not weaken the assertions.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check --fix pr_dashboard/comments.py tests/unittest/test_pr_dashboard_comments.py
uv run pre-commit run --files pr_dashboard/comments.py tests/unittest/test_pr_dashboard_comments.py
git add pr_dashboard/comments.py tests/unittest/test_pr_dashboard_comments.py tests/unittest/fixtures/pr_dashboard
git commit -m "feat(dashboard): identify PR-Agent comments by identity marker and parse findings"
```

---

### Task 5: Provider read layer

**Files:**
- Create: `pr_dashboard/providers.py`
- Test: `tests/unittest/test_pr_dashboard_providers.py`

**Interfaces:**
- Consumes: `pr_dashboard.registry.Repo`, `pr_dashboard.comments` (`classify`, `parse_findings`, `CommentKind`), `pr_dashboard.store` (for `provider_cache`), `pr_agent.config_loader.get_settings`.
- Produces:
  - `class CredentialStatus` — frozen dataclass `provider: str`, `configured: bool`, `detail: str`
  - `credential_status(provider: str) -> CredentialStatus`
  - `class PullRequestSummary` — frozen dataclass `number: int`, `title: str`, `author: str`, `state: str`, `url: str`, `updated_at: str`
  - `class ReviewComment` — frozen dataclass `kind: comments.CommentKind`, `body: str`, `created_at: str`, `url: str`
  - `class ProviderError(RuntimeError)` with attributes `status: int | None`, `retry_after: str | None`
  - `list_pull_requests(repo: registry.Repo, state: str = "open", limit: int = 50) -> list[PullRequestSummary]`
  - `list_pr_agent_comments(repo: registry.Repo, number: int) -> list[ReviewComment]`
  - `cached(conn, key: str, ttl_seconds: int, fetch: Callable[[], object]) -> tuple[object, bool]` returning `(payload, is_stale)`

- [ ] **Step 1: Write the failing test**

Create `tests/unittest/test_pr_dashboard_providers.py`:

```python
import json

import pytest

from pr_agent.algo.utils import PRReviewIdentity
from pr_dashboard import comments, providers, registry, store


class TestCredentialStatus:
    def test_github_user_token_present(self, monkeypatch):
        """A configured GitHub user token reports configured with no secret in the detail"""
        monkeypatch.setattr(providers, "_setting", lambda key, default=None: {
            "GITHUB.DEPLOYMENT_TYPE": "user",
            "GITHUB.USER_TOKEN": "ghp_supersecret",
        }.get(key, default))
        status = providers.credential_status("github")
        assert status.configured is True
        assert "ghp_supersecret" not in status.detail

    def test_github_token_missing(self, monkeypatch):
        """A missing GitHub token is reported per provider, not as a generic failure"""
        monkeypatch.setattr(providers, "_setting", lambda key, default=None: {
            "GITHUB.DEPLOYMENT_TYPE": "user",
        }.get(key, default))
        status = providers.credential_status("github")
        assert status.configured is False
        assert "github" in status.detail.lower()

    def test_bitbucket_bearer_token(self, monkeypatch):
        """Bitbucket bearer auth is detected from BITBUCKET.BEARER_TOKEN"""
        monkeypatch.setattr(providers, "_setting", lambda key, default=None: {
            "BITBUCKET.AUTH_TYPE": "bearer",
            "BITBUCKET.BEARER_TOKEN": "secret",
        }.get(key, default))
        assert providers.credential_status("bitbucket").configured is True

    def test_bitbucket_basic_token(self, monkeypatch):
        """Bitbucket basic auth is detected from BITBUCKET.BASIC_TOKEN"""
        monkeypatch.setattr(providers, "_setting", lambda key, default=None: {
            "BITBUCKET.AUTH_TYPE": "basic",
            "BITBUCKET.BASIC_TOKEN": "secret",
        }.get(key, default))
        assert providers.credential_status("bitbucket").configured is True

    def test_unknown_provider(self):
        """An unsupported provider is reported, not crashed on"""
        assert providers.credential_status("gitlab").configured is False


class TestCache:
    def test_miss_then_hit(self, tmp_path):
        """A second call inside the TTL does not refetch"""
        conn = store.connect(tmp_path / "usage.db")
        calls = []

        def fetch():
            calls.append(1)
            return {"value": len(calls)}

        first, stale_first = providers.cached(conn, "k", 60, fetch)
        second, stale_second = providers.cached(conn, "k", 60, fetch)
        assert first == second == {"value": 1}
        assert stale_first is False and stale_second is False
        assert len(calls) == 1

    def test_expired_entry_refetches(self, tmp_path):
        """Past the TTL the value is refetched"""
        conn = store.connect(tmp_path / "usage.db")
        conn.execute(
            "INSERT INTO provider_cache (key, fetched_at, expires_at, payload) VALUES (?, ?, ?, ?)",
            ("k", "2020-01-01T00:00:00+00:00", "2020-01-01T00:01:00+00:00", json.dumps({"old": True})),
        )
        value, stale = providers.cached(conn, "k", 60, lambda: {"new": True})
        assert value == {"new": True}
        assert stale is False

    def test_stale_entry_served_when_fetch_fails(self, tmp_path):
        """When the provider is unreachable, expired data is served and flagged stale"""
        conn = store.connect(tmp_path / "usage.db")
        conn.execute(
            "INSERT INTO provider_cache (key, fetched_at, expires_at, payload) VALUES (?, ?, ?, ?)",
            ("k", "2020-01-01T00:00:00+00:00", "2020-01-01T00:01:00+00:00", json.dumps({"old": True})),
        )

        def fetch():
            raise providers.ProviderError("429 rate limited", status=429, retry_after="60")

        value, stale = providers.cached(conn, "k", 60, fetch)
        assert value == {"old": True}
        assert stale is True

    def test_fetch_failure_with_no_cache_raises(self, tmp_path):
        """With nothing cached, a provider failure surfaces instead of showing empty data"""
        conn = store.connect(tmp_path / "usage.db")

        def fetch():
            raise providers.ProviderError("401 unauthorized", status=401, retry_after=None)

        with pytest.raises(providers.ProviderError):
            providers.cached(conn, "k", 60, fetch)


class TestCommentFiltering:
    def test_only_pr_agent_comments_are_returned(self, monkeypatch):
        """Human comments are filtered out of the review list"""
        raw = [
            {"body": "looks good", "created_at": "2026-09-01T00:00:00Z", "html_url": "u1"},
            {"body": f"{PRReviewIdentity.REGULAR.value}\n## PR Reviewer Guide\n- **Bug** `a.py` [10-12]",
             "created_at": "2026-09-02T00:00:00Z", "html_url": "u2"},
        ]
        monkeypatch.setattr(providers, "_fetch_github_issue_comments", lambda repo, number: raw)
        result = providers.list_pr_agent_comments(registry.Repo("github", "o/r"), 1)
        assert [c.url for c in result] == ["u2"]
        assert result[0].kind is comments.CommentKind.REVIEW
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_providers.py -q`

Expected: FAIL — `ImportError: cannot import name 'providers' from 'pr_dashboard'`

- [ ] **Step 3: Implement the provider layer**

Create `pr_dashboard/providers.py`:

```python
"""Read pull requests and PR-Agent comments from GitHub and Bitbucket.

Credentials are resolved through pr_agent's own settings so that the dashboard adds no new
place for a token to live. The GitHub path uses PyGithub, already a dependency. The
Bitbucket path uses the REST API directly with the same auth headers
pr_agent/git_providers/bitbucket_provider.py builds, because the atlassian Cloud client is
pull-request-scoped and offers no repository-level listing.

pr_agent's GitProvider interface is deliberately not used here: it is constructed from a
single pull-request URL and has no concept of listing a repository's pull requests.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import requests
from github import Auth, Github

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_dashboard import comments as comments_module
from pr_dashboard import registry

BITBUCKET_API = "https://api.bitbucket.org/2.0"
DEFAULT_TTL_SECONDS = 120


class ProviderError(RuntimeError):
    """A provider call failed in a way the interface must show rather than swallow."""

    def __init__(self, message: str, *, status: Optional[int] = None, retry_after: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class CredentialStatus:
    provider: str
    configured: bool
    detail: str


@dataclass(frozen=True)
class PullRequestSummary:
    number: int
    title: str
    author: str
    state: str
    url: str
    updated_at: str


@dataclass(frozen=True)
class ReviewComment:
    kind: comments_module.CommentKind
    body: str
    created_at: str
    url: str


def _setting(key: str, default=None):
    """Single indirection over get_settings so tests can substitute credentials."""
    return get_settings().get(key, default)


def credential_status(provider: str) -> CredentialStatus:
    """Report whether a provider is usable, never echoing the credential itself."""
    if provider == "github":
        deployment = _setting("GITHUB.DEPLOYMENT_TYPE", "user")
        if deployment == "app":
            configured = bool(_setting("GITHUB.APP_ID")) and bool(_setting("GITHUB.PRIVATE_KEY"))
            detail = "github app credentials configured" if configured else (
                "github app deployment needs GITHUB.APP_ID and GITHUB.PRIVATE_KEY")
            return CredentialStatus("github", configured, detail)
        configured = bool(_setting("GITHUB.USER_TOKEN"))
        detail = "github user token configured" if configured else (
            "no token configured for github; set GITHUB.USER_TOKEN in .secrets.toml")
        return CredentialStatus("github", configured, detail)

    if provider == "bitbucket":
        auth_type = _setting("BITBUCKET.AUTH_TYPE", "bearer")
        key = "BITBUCKET.BASIC_TOKEN" if auth_type == "basic" else "BITBUCKET.BEARER_TOKEN"
        configured = bool(_setting(key))
        detail = f"bitbucket {auth_type} token configured" if configured else (
            f"no token configured for bitbucket; set {key} in .secrets.toml")
        return CredentialStatus("bitbucket", configured, detail)

    return CredentialStatus(provider, False, f"unsupported provider {provider!r}")


def cached(conn, key: str, ttl_seconds: int, fetch: Callable[[], object]) -> tuple[object, bool]:
    """Return (payload, is_stale). Serves expired data when the provider is unreachable."""
    now = datetime.now(timezone.utc)
    row = conn.execute("SELECT payload, expires_at FROM provider_cache WHERE key = ?", (key,)).fetchone()
    if row is not None and datetime.fromisoformat(row["expires_at"]) > now:
        return json.loads(row["payload"]), False

    try:
        payload = fetch()
    except ProviderError:
        if row is not None:
            get_logger().warning(f"pr_dashboard: serving stale cache for {key}")
            return json.loads(row["payload"]), True
        raise

    conn.execute(
        "INSERT OR REPLACE INTO provider_cache (key, fetched_at, expires_at, payload) VALUES (?, ?, ?, ?)",
        (key, now.isoformat(), (now + timedelta(seconds=ttl_seconds)).isoformat(), json.dumps(payload)),
    )
    return payload, False


def _github_client() -> Github:
    status = credential_status("github")
    if not status.configured:
        raise ProviderError(status.detail)
    return Github(auth=Auth.Token(_setting("GITHUB.USER_TOKEN")))


def _bitbucket_headers() -> dict:
    status = credential_status("bitbucket")
    if not status.configured:
        raise ProviderError(status.detail)
    auth_type = _setting("BITBUCKET.AUTH_TYPE", "bearer")
    if auth_type == "basic":
        return {"Authorization": f"Basic {_setting('BITBUCKET.BASIC_TOKEN')}"}
    return {"Authorization": f"Bearer {_setting('BITBUCKET.BEARER_TOKEN')}"}


def _bitbucket_get(path: str, params: Optional[dict] = None) -> dict:
    response = requests.get(
        f"{BITBUCKET_API}{path}", headers=_bitbucket_headers(), params=params or {}, timeout=30)
    if response.status_code >= 400:
        raise ProviderError(
            f"bitbucket returned {response.status_code} for {path}",
            status=response.status_code,
            retry_after=response.headers.get("Retry-After"),
        )
    return response.json()


def _fetch_github_pull_requests(repo: registry.Repo, state: str, limit: int) -> list[dict]:
    try:
        pulls = _github_client().get_repo(repo.slug).get_pulls(state=state, sort="updated", direction="desc")
        return [
            {
                "number": pull.number,
                "title": pull.title,
                "author": pull.user.login if pull.user else "",
                "state": pull.state,
                "url": pull.html_url,
                "updated_at": pull.updated_at.isoformat() if pull.updated_at else "",
            }
            for pull in pulls[:limit]
        ]
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 - PyGithub raises a wide range of transport errors
        status = getattr(exc, "status", None)
        raise ProviderError(f"github: {exc}", status=status) from exc


def _fetch_bitbucket_pull_requests(repo: registry.Repo, state: str, limit: int) -> list[dict]:
    bitbucket_state = {"open": "OPEN", "closed": "MERGED", "all": None}.get(state, "OPEN")
    params = {"pagelen": min(limit, 50)}
    if bitbucket_state:
        params["state"] = bitbucket_state
    payload = _bitbucket_get(f"/repositories/{repo.slug}/pullrequests", params)
    return [
        {
            "number": item["id"],
            "title": item.get("title", ""),
            "author": (item.get("author") or {}).get("display_name", ""),
            "state": item.get("state", ""),
            "url": ((item.get("links") or {}).get("html") or {}).get("href", ""),
            "updated_at": item.get("updated_on", ""),
        }
        for item in payload.get("values", [])[:limit]
    ]


def list_pull_requests(repo: registry.Repo, state: str = "open", limit: int = 50) -> list[PullRequestSummary]:
    """Return a repository's pull requests, newest update first."""
    if repo.provider == "github":
        raw = _fetch_github_pull_requests(repo, state, limit)
    elif repo.provider == "bitbucket":
        raw = _fetch_bitbucket_pull_requests(repo, state, limit)
    else:
        raise ProviderError(f"unsupported provider {repo.provider!r}")
    return [PullRequestSummary(**item) for item in raw]


def _fetch_github_issue_comments(repo: registry.Repo, number: int) -> list[dict]:
    try:
        pull = _github_client().get_repo(repo.slug).get_pull(number)
        return [
            {"body": c.body or "", "created_at": c.created_at.isoformat() if c.created_at else "",
             "html_url": c.html_url}
            for c in pull.get_issue_comments()
        ]
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 - see _fetch_github_pull_requests
        raise ProviderError(f"github: {exc}", status=getattr(exc, "status", None)) from exc


def _fetch_bitbucket_comments(repo: registry.Repo, number: int) -> list[dict]:
    payload = _bitbucket_get(f"/repositories/{repo.slug}/pullrequests/{number}/comments", {"pagelen": 50})
    return [
        {
            "body": ((item.get("content") or {}).get("raw") or ""),
            "created_at": item.get("created_on", ""),
            "html_url": ((item.get("links") or {}).get("html") or {}).get("href", ""),
        }
        for item in payload.get("values", [])
    ]


def list_pr_agent_comments(repo: registry.Repo, number: int) -> list[ReviewComment]:
    """Return only the comments PR-Agent published on a pull request."""
    if repo.provider == "github":
        raw = _fetch_github_issue_comments(repo, number)
    elif repo.provider == "bitbucket":
        raw = _fetch_bitbucket_comments(repo, number)
    else:
        raise ProviderError(f"unsupported provider {repo.provider!r}")

    result = []
    for item in raw:
        kind = comments_module.classify(item["body"])
        if kind is comments_module.CommentKind.OTHER:
            continue
        result.append(ReviewComment(
            kind=kind, body=item["body"], created_at=item["created_at"], url=item["html_url"]))
    return result
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_providers.py -q`

Expected: PASS, 10 tests

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check --fix pr_dashboard/providers.py tests/unittest/test_pr_dashboard_providers.py
uv run pre-commit run --files pr_dashboard/providers.py tests/unittest/test_pr_dashboard_providers.py
git add pr_dashboard/providers.py tests/unittest/test_pr_dashboard_providers.py
git commit -m "feat(dashboard): read pull requests and PR-Agent comments from GitHub and Bitbucket"
```

---

### Task 6: Application shell, repository management page, console script

**Files:**
- Create: `pr_dashboard/app.py`
- Create: `pr_dashboard/templates/base.html`
- Create: `pr_dashboard/templates/repos.html`
- Create: `pr_dashboard/templates/_repo_rows.html`
- Create: `pr_dashboard/static/.gitkeep`
- Modify: `pyproject.toml` (`[project.scripts]` only)
- Test: `tests/unittest/test_pr_dashboard_app.py`

**Interfaces:**
- Consumes: `pr_dashboard.registry`, `pr_dashboard.providers`, `pr_dashboard.store`.
- Produces:
  - `app: fastapi.FastAPI`
  - `create_app(*, registry_path=None, db_path=None) -> fastapi.FastAPI`
  - `main() -> None` — the `pr-dashboard` console entry point
  - Routes `GET /repos`, `POST /repos`, `POST /repos/{provider}/{slug:path}/delete`

**Vendored assets:** download into `pr_dashboard/static/` and commit them. Do not add a
`package.json`.

```bash
mkdir -p pr_dashboard/static
curl -fsSL https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js -o pr_dashboard/static/htmx.min.js
curl -fsSL https://unpkg.com/alpinejs@3.14.9/dist/cdn.min.js -o pr_dashboard/static/alpine.min.js
curl -fsSL https://unpkg.com/chart.js@4.4.7/dist/chart.umd.js -o pr_dashboard/static/chart.umd.js
```

- [ ] **Step 1: Write the failing test**

Create `tests/unittest/test_pr_dashboard_app.py`:

```python
from fastapi.testclient import TestClient

from pr_dashboard import app as app_module
from pr_dashboard import providers


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "credential_status", lambda provider: providers.CredentialStatus(
        provider, True, f"{provider} token configured"))
    application = app_module.create_app(
        registry_path=tmp_path / "pr_dashboard.toml", db_path=tmp_path / "usage.db")
    return TestClient(application)


class TestReposPage:
    def test_empty_registry_renders_guidance(self, tmp_path, monkeypatch):
        """With nothing registered the page explains how to add a repository"""
        response = _client(tmp_path, monkeypatch).get("/repos")
        assert response.status_code == 200
        assert "add a repository" in response.text.lower()

    def test_add_repository(self, tmp_path, monkeypatch):
        """Posting a valid repository registers it and it appears in the list"""
        client = _client(tmp_path, monkeypatch)
        response = client.post("/repos", data={"provider": "github", "slug": "samer2373/block_rush"})
        assert response.status_code == 200
        assert "samer2373/block_rush" in response.text

    def test_add_invalid_slug_shows_the_error(self, tmp_path, monkeypatch):
        """An invalid slug is reported in the page, not raised as a 500"""
        client = _client(tmp_path, monkeypatch)
        response = client.post("/repos", data={"provider": "github", "slug": "no-owner"})
        assert response.status_code == 200
        assert "owner/name" in response.text

    def test_delete_repository(self, tmp_path, monkeypatch):
        """A registered repository can be removed"""
        client = _client(tmp_path, monkeypatch)
        client.post("/repos", data={"provider": "github", "slug": "o/r"})
        response = client.post("/repos/github/o/r/delete")
        assert response.status_code == 200
        assert "o/r" not in response.text

    def test_credential_status_is_shown_without_the_token(self, tmp_path, monkeypatch):
        """The page reports credential state and never renders the secret"""
        monkeypatch.setattr(providers, "credential_status", lambda provider: providers.CredentialStatus(
            provider, False, "no token configured for bitbucket; set BITBUCKET.BEARER_TOKEN in .secrets.toml"))
        application = app_module.create_app(
            registry_path=tmp_path / "pr_dashboard.toml", db_path=tmp_path / "usage.db")
        client = TestClient(application)
        client.post("/repos", data={"provider": "bitbucket", "slug": "team/svc"})
        response = client.get("/repos")
        assert "no token configured for bitbucket" in response.text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_app.py -q`

Expected: FAIL — `ImportError: cannot import name 'app' from 'pr_dashboard'`

- [ ] **Step 3: Create the base template**

Create `pr_dashboard/templates/base.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}PR-Agent Dashboard{% endblock %}</title>
  <script src="{{ url_for('static', path='/htmx.min.js') }}"></script>
  <script defer src="{{ url_for('static', path='/alpine.min.js') }}"></script>
  <style>
    :root { color-scheme: light dark; --border: #8883; }
    body { margin: 0; font: 14px/1.5 system-ui, sans-serif; }
    header { display: flex; gap: 1.5rem; padding: 1rem 1.5rem; border-bottom: 1px solid var(--border); }
    header a { text-decoration: none; }
    main { padding: 1.5rem; }
    table { border-collapse: collapse; width: 100%; }
    th, td { text-align: left; padding: .5rem .75rem; border-bottom: 1px solid var(--border); }
    .error { padding: .75rem 1rem; border: 1px solid #c0392b; border-radius: 4px; margin-bottom: 1rem; }
    .stale { padding: .75rem 1rem; border: 1px solid #d68910; border-radius: 4px; margin-bottom: 1rem; }
    .muted { opacity: .65; }
  </style>
</head>
<body>
  <header>
    <a href="/">Overview</a>
    <a href="/repos">Repositories</a>
    <a href="/usage">Usage</a>
  </header>
  <main>
    {% block content %}{% endblock %}
  </main>
</body>
</html>
```

- [ ] **Step 4: Create the repositories templates**

Create `pr_dashboard/templates/_repo_rows.html`:

```html
{% if error %}<div class="error">{{ error }}</div>{% endif %}
{% if repos %}
<table>
  <thead><tr><th>Provider</th><th>Repository</th><th>Credentials</th><th></th></tr></thead>
  <tbody>
  {% for row in repos %}
    <tr>
      <td>{{ row.repo.provider }}</td>
      <td><a href="/repos/{{ row.repo.provider }}/{{ row.repo.slug }}">{{ row.repo.slug }}</a></td>
      <td class="{{ '' if row.credentials.configured else 'muted' }}">{{ row.credentials.detail }}</td>
      <td>
        <button hx-post="/repos/{{ row.repo.provider }}/{{ row.repo.slug }}/delete"
                hx-target="#repo-list" hx-swap="innerHTML">Remove</button>
      </td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% else %}
<p class="muted">No repositories yet. Add a repository using the form above.</p>
{% endif %}
```

Create `pr_dashboard/templates/repos.html`:

```html
{% extends "base.html" %}
{% block title %}Repositories{% endblock %}
{% block content %}
<h1>Repositories</h1>
<form hx-post="/repos" hx-target="#repo-list" hx-swap="innerHTML">
  <select name="provider">
    <option value="github">github</option>
    <option value="bitbucket">bitbucket</option>
  </select>
  <input name="slug" placeholder="owner/name" required>
  <button type="submit">Add</button>
</form>
<p class="muted">Credentials are read from your existing .secrets.toml and environment. They are never stored here.</p>
<div id="repo-list">
  {% include "_repo_rows.html" %}
</div>
{% endblock %}
```

- [ ] **Step 5: Implement the application**

Create `pr_dashboard/app.py`:

```python
"""FastAPI application for the PR-Agent dashboard.

create_app takes explicit paths so tests can run against a temporary registry and
database; the module-level ``app`` uses the real defaults for uvicorn.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pr_dashboard import providers, registry, store

_HERE = Path(__file__).parent


def create_app(*, registry_path: Optional[Path] = None, db_path: Optional[Path] = None) -> FastAPI:
    application = FastAPI(title="PR-Agent Dashboard")
    application.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    templates = Jinja2Templates(directory=str(_HERE / "templates"))

    application.state.registry_path = registry_path or registry.DEFAULT_REGISTRY_PATH
    application.state.db_path = db_path or store.DEFAULT_DB_PATH
    application.state.templates = templates

    def repo_rows() -> list[dict]:
        return [
            {"repo": repo, "credentials": providers.credential_status(repo.provider)}
            for repo in registry.load(application.state.registry_path)
        ]

    def render_rows(request: Request, error: Optional[str] = None) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "_repo_rows.html", {"repos": repo_rows(), "error": error})

    @application.get("/repos", response_class=HTMLResponse)
    def repos_page(request: Request):
        return templates.TemplateResponse(request, "repos.html", {"repos": repo_rows(), "error": None})

    @application.post("/repos", response_class=HTMLResponse)
    def add_repo(request: Request, provider: str = Form(...), slug: str = Form(...)):
        try:
            registry.add(registry.Repo(provider=provider, slug=slug), application.state.registry_path)
        except registry.RegistryError as exc:
            return render_rows(request, error=str(exc))
        return render_rows(request)

    @application.post("/repos/{provider}/{slug:path}/delete", response_class=HTMLResponse)
    def delete_repo(request: Request, provider: str, slug: str):
        try:
            registry.remove(provider, slug, application.state.registry_path)
        except registry.RegistryError as exc:
            return render_rows(request, error=str(exc))
        return render_rows(request)

    return application


app = create_app()


def main() -> None:
    """Console entry point for the pr-dashboard script."""
    import uvicorn

    uvicorn.run("pr_dashboard.app:app", host="127.0.0.1", port=8420, reload=False)
```

- [ ] **Step 6: Register the console script**

In `pyproject.toml`, add one line to the existing `[project.scripts]` table, leaving every
other entry untouched:

```toml
pr-dashboard = "pr_dashboard.app:main"
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_app.py -q`

Expected: PASS, 5 tests

- [ ] **Step 8: Start it once by hand**

```bash
uv run uvicorn pr_dashboard.app:app --port 8420
```

Open `http://127.0.0.1:8420/repos`, add a repository, confirm it appears with its
credential status, remove it, then stop the server with Ctrl-C.

- [ ] **Step 9: Lint and commit**

```bash
uv run ruff check --fix pr_dashboard/app.py tests/unittest/test_pr_dashboard_app.py
uv run pre-commit run --files pr_dashboard/app.py pyproject.toml tests/unittest/test_pr_dashboard_app.py
git add pr_dashboard/app.py pr_dashboard/templates pr_dashboard/static pyproject.toml tests/unittest/test_pr_dashboard_app.py
git commit -m "feat(dashboard): add FastAPI shell, repository management page, and console script"
```

---

### Task 7: Overview, pull request list, pull request detail

**Files:**
- Modify: `pr_dashboard/app.py` (add three routes and a helper)
- Create: `pr_dashboard/templates/overview.html`
- Create: `pr_dashboard/templates/repo_detail.html`
- Create: `pr_dashboard/templates/pr_detail.html`
- Test: `tests/unittest/test_pr_dashboard_views.py`

**Interfaces:**
- Consumes: `pr_dashboard.providers` (`list_pull_requests`, `list_pr_agent_comments`, `cached`, `ProviderError`), `pr_dashboard.comments.parse_findings`, `pr_dashboard.store`.
- Produces: routes `GET /`, `GET /repos/{provider}/{slug:path}`, `GET /pr/{provider}/{slug:path}/{number}`; helper `repo_summary(conn, repo) -> dict` with keys `repo`, `open_prs`, `reviewed_prs`, `last_run`, `tokens_7d`, `cost_7d`.

- [ ] **Step 1: Write the failing test**

Create `tests/unittest/test_pr_dashboard_views.py`:

```python
from fastapi.testclient import TestClient

from pr_agent.algo.utils import PRReviewIdentity
from pr_dashboard import app as app_module
from pr_dashboard import comments, providers, registry

REVIEW_BODY = (
    f"{PRReviewIdentity.REGULAR.value}\n"
    "## PR Reviewer Guide\n\n"
    "- **Race on profile write** `lib/profile.dart` [120-134]\n"
    "- **Missing ads timeout** `lib/ads.dart` [42-58]\n"
)


def _client(tmp_path, monkeypatch, *, pulls=None, review_comments=None, error=None):
    monkeypatch.setattr(providers, "credential_status", lambda provider: providers.CredentialStatus(
        provider, True, "configured"))

    def fake_pulls(repo, state="open", limit=50):
        if error:
            raise error
        return pulls or []

    def fake_comments(repo, number):
        if error:
            raise error
        return review_comments or []

    monkeypatch.setattr(providers, "list_pull_requests", fake_pulls)
    monkeypatch.setattr(providers, "list_pr_agent_comments", fake_comments)

    application = app_module.create_app(
        registry_path=tmp_path / "pr_dashboard.toml", db_path=tmp_path / "usage.db")
    client = TestClient(application)
    client.post("/repos", data={"provider": "github", "slug": "samer2373/block_rush"})
    return client


class TestOverview:
    def test_lists_every_registered_repository(self, tmp_path, monkeypatch):
        """The overview shows one card per registered repository"""
        client = _client(tmp_path, monkeypatch, pulls=[
            providers.PullRequestSummary(1, "Add rush mode", "samer2373", "open",
                                         "https://github.com/samer2373/block_rush/pull/1", "2026-09-10T10:00:00")
        ])
        response = client.get("/")
        assert response.status_code == 200
        assert "samer2373/block_rush" in response.text

    def test_provider_error_renders_a_banner(self, tmp_path, monkeypatch):
        """A provider failure shows a banner instead of a stack trace"""
        client = _client(tmp_path, monkeypatch,
                         error=providers.ProviderError("github returned 429", status=429, retry_after="60"))
        response = client.get("/")
        assert response.status_code == 200
        assert "429" in response.text
        assert "Traceback" not in response.text


class TestRepoDetail:
    def test_lists_pull_requests(self, tmp_path, monkeypatch):
        """The repository page lists pull requests with title and author"""
        client = _client(tmp_path, monkeypatch, pulls=[
            providers.PullRequestSummary(1, "Add rush mode", "samer2373", "open",
                                         "https://github.com/samer2373/block_rush/pull/1", "2026-09-10T10:00:00")
        ])
        response = client.get("/repos/github/samer2373/block_rush")
        assert "Add rush mode" in response.text
        assert "samer2373" in response.text


class TestPrDetail:
    def test_renders_findings_with_file_and_lines(self, tmp_path, monkeypatch):
        """The pull request page shows each finding with its file path and line range"""
        client = _client(tmp_path, monkeypatch, review_comments=[
            providers.ReviewComment(kind=comments.CommentKind.REVIEW, body=REVIEW_BODY,
                                    created_at="2026-09-10T10:00:00", url="https://example/c1")
        ])
        response = client.get("/pr/github/samer2373/block_rush/1")
        assert response.status_code == 200
        assert "Race on profile write" in response.text
        assert "lib/profile.dart" in response.text
        assert "120" in response.text and "134" in response.text

    def test_run_history_for_the_pull_request(self, tmp_path, monkeypatch):
        """Recorded runs for this pull request appear on its page"""
        from pr_dashboard import store
        client = _client(tmp_path, monkeypatch)
        conn = store.connect(tmp_path / "usage.db")
        run_id = store.start_run(
            conn, provider="github", command="review",
            pr_url="https://github.com/samer2373/block_rush/pull/1",
            repo_slug="samer2373/block_rush", pr_number=1, started_at="2026-09-10T10:00:00+00:00")
        conn.execute("UPDATE runs SET status='ok', total_tokens=4321, model_used='gemini/flash' WHERE id=?",
                     (run_id,))
        response = client.get("/pr/github/samer2373/block_rush/1")
        assert "4321" in response.text
        assert "gemini/flash" in response.text

    def test_unregistered_repository_is_404(self, tmp_path, monkeypatch):
        """A pull request under an unregistered repository is not found"""
        client = _client(tmp_path, monkeypatch)
        assert client.get("/pr/github/someone/else/1").status_code == 404
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_views.py -q`

Expected: FAIL — 404 on `/` and on the detail routes

- [ ] **Step 3: Create the three templates**

Create `pr_dashboard/templates/overview.html`:

```html
{% extends "base.html" %}
{% block title %}Overview{% endblock %}
{% block content %}
<h1>Overview</h1>
{% if error %}<div class="error">{{ error }}</div>{% endif %}
{% if stale %}<div class="stale">Showing cached data; the provider was unreachable.</div>{% endif %}
{% if not cards %}<p class="muted">No repositories yet. <a href="/repos">Add one</a>.</p>{% endif %}
<table>
  <thead><tr><th>Repository</th><th>Open PRs</th><th>Reviewed</th><th>Last run</th>
    <th>Tokens (7d)</th><th>Cost (7d)</th></tr></thead>
  <tbody>
  {% for card in cards %}
    <tr>
      <td><a href="/repos/{{ card.repo.provider }}/{{ card.repo.slug }}">{{ card.repo.slug }}</a></td>
      <td>{{ card.open_prs if card.open_prs is not none else "—" }}</td>
      <td>{{ card.reviewed_prs if card.reviewed_prs is not none else "—" }}</td>
      <td>{{ card.last_run or "—" }}</td>
      <td>{{ card.tokens_7d or "—" }}</td>
      <td>{{ card.cost_7d if card.cost_7d is not none else "not reported" }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
```

Create `pr_dashboard/templates/repo_detail.html`:

```html
{% extends "base.html" %}
{% block title %}{{ repo.slug }}{% endblock %}
{% block content %}
<h1>{{ repo.slug }} <span class="muted">{{ repo.provider }}</span></h1>
{% if error %}<div class="error">{{ error }}</div>{% endif %}
{% if stale %}<div class="stale">Showing cached data; the provider was unreachable.</div>{% endif %}
<table>
  <thead><tr><th>#</th><th>Title</th><th>Author</th><th>State</th><th>Updated</th></tr></thead>
  <tbody>
  {% for pull in pulls %}
    <tr>
      <td><a href="/pr/{{ repo.provider }}/{{ repo.slug }}/{{ pull.number }}">{{ pull.number }}</a></td>
      <td>{{ pull.title }}</td>
      <td>{{ pull.author }}</td>
      <td>{{ pull.state }}</td>
      <td>{{ pull.updated_at }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
```

Create `pr_dashboard/templates/pr_detail.html`:

```html
{% extends "base.html" %}
{% block title %}{{ repo.slug }} #{{ number }}{% endblock %}
{% block content %}
<h1>{{ repo.slug }} #{{ number }}</h1>
{% if error %}<div class="error">{{ error }}</div>{% endif %}

<h2>Findings</h2>
{% if not findings %}<p class="muted">No findings parsed from PR-Agent's comments.</p>{% endif %}
<table>
  <thead><tr><th>Finding</th><th>File</th><th>Lines</th></tr></thead>
  <tbody>
  {% for finding in findings %}
    <tr>
      <td>{{ finding.title }}</td>
      <td>{{ finding.relevant_file or "—" }}</td>
      <td>{% if finding.line_range %}{{ finding.line_range[0] }}–{{ finding.line_range[1] }}{% else %}—{% endif %}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>

<h2>Runs</h2>
{% if not runs %}<p class="muted">No recorded runs. Set pr_dashboard.record_runs = true to collect usage.</p>{% endif %}
<table>
  <thead><tr><th>Started</th><th>Command</th><th>Status</th><th>Model</th><th>Tokens</th><th>Cost</th></tr></thead>
  <tbody>
  {% for run in runs %}
    <tr>
      <td>{{ run["started_at"] }}</td>
      <td>{{ run["command"] }}</td>
      <td>{{ run["status"] }}</td>
      <td>{{ run["model_used"] or "—" }}</td>
      <td>{{ run["total_tokens"] if run["total_tokens"] is not none else "not reported" }}</td>
      <td>{{ run["total_cost_usd"] or "not reported" }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>

<h2>Comments</h2>
{% for comment in review_comments %}
  <details>
    <summary>{{ comment.kind.value }} — {{ comment.created_at }}</summary>
    <pre>{{ comment.body }}</pre>
  </details>
{% endfor %}
{% endblock %}
```

- [ ] **Step 4: Add the three routes**

In `pr_dashboard/app.py`, extend the imports:

```python
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from pr_dashboard import comments as comments_module
```

Then add inside `create_app`, before `return application`:

```python
    def find_repo(provider: str, slug: str) -> registry.Repo:
        for repo in registry.load(application.state.registry_path):
            if repo.provider == provider and repo.slug == slug:
                return repo
        raise HTTPException(status_code=404, detail=f"{provider}:{slug} is not registered")

    def repo_summary(conn, repo: registry.Repo) -> dict:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        row = conn.execute(
            "SELECT sum(total_tokens) AS tokens, max(started_at) AS last_run "
            "FROM runs WHERE provider = ? AND repo_slug = ? AND started_at >= ?",
            (repo.provider, repo.slug, since),
        ).fetchone()
        costs = conn.execute(
            "SELECT total_cost_usd FROM runs "
            "WHERE provider = ? AND repo_slug = ? AND started_at >= ? AND total_cost_usd IS NOT NULL",
            (repo.provider, repo.slug, since),
        ).fetchall()
        total_cost = sum((Decimal(r["total_cost_usd"]) for r in costs), Decimal("0")) if costs else None
        return {
            "repo": repo,
            "open_prs": None,
            "reviewed_prs": None,
            "last_run": row["last_run"],
            "tokens_7d": row["tokens"],
            "cost_7d": total_cost,
        }

    @application.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        conn = store.connect(application.state.db_path)
        cards, error, stale = [], None, False
        for repo in registry.load(application.state.registry_path):
            card = repo_summary(conn, repo)
            try:
                pulls = providers.list_pull_requests(repo, state="open", limit=50)
                card["open_prs"] = len(pulls)
            except providers.ProviderError as exc:
                error = str(exc)
            cards.append(card)
        return templates.TemplateResponse(
            request, "overview.html", {"cards": cards, "error": error, "stale": stale})

    @application.get("/repos/{provider}/{slug:path}", response_class=HTMLResponse)
    def repo_detail(request: Request, provider: str, slug: str):
        repo = find_repo(provider, slug)
        pulls, error = [], None
        try:
            pulls = providers.list_pull_requests(repo, state="open", limit=50)
        except providers.ProviderError as exc:
            error = str(exc)
        return templates.TemplateResponse(
            request, "repo_detail.html", {"repo": repo, "pulls": pulls, "error": error, "stale": False})

    @application.get("/pr/{provider}/{slug:path}/{number}", response_class=HTMLResponse)
    def pr_detail(request: Request, provider: str, slug: str, number: int):
        repo = find_repo(provider, slug)
        review_comments, findings, error = [], [], None
        try:
            review_comments = providers.list_pr_agent_comments(repo, number)
            for comment in review_comments:
                findings.extend(comments_module.parse_findings(comment.body))
        except providers.ProviderError as exc:
            error = str(exc)
        conn = store.connect(application.state.db_path)
        runs = conn.execute(
            "SELECT * FROM runs WHERE provider = ? AND repo_slug = ? AND pr_number = ? "
            "ORDER BY started_at DESC",
            (repo.provider, repo.slug, number),
        ).fetchall()
        return templates.TemplateResponse(request, "pr_detail.html", {
            "repo": repo, "number": number, "findings": findings,
            "review_comments": review_comments, "runs": runs, "error": error,
        })
```

Add `from decimal import Decimal` to the module imports.

**Route order note:** `/repos/{provider}/{slug:path}` must be registered *after* the
literal `POST /repos` and `GET /repos` routes from Task 6, and the `{slug:path}` converter
is required because a slug contains a slash.

`{slug:path}` followed by another segment is safe here — verified against the project's
own starlette before this plan was written:

```
/pr/{provider}/{slug:path}/{number}
  -> ^/pr/(?P<provider>[^/]+)/(?P<slug>.*)/(?P<number>[^/]+)$
  /pr/github/samer2373/block_rush/1
  -> {"provider": "github", "slug": "samer2373/block_rush", "number": "1"}
```

The `.*` is greedy but the trailing `[^/]+$` forces it to backtrack, so the number is
captured correctly. Do not restructure these routes to avoid a problem that does not
exist; the 404 test in this task covers the behaviour.

- [ ] **Step 5: Run the test to verify it passes**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_views.py -q`

Expected: PASS, 6 tests

- [ ] **Step 6: Confirm the repository page still works**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_app.py tests/unittest/test_pr_dashboard_views.py -q`

Expected: PASS, 11 tests. If `/repos` now resolves to `repo_detail`, move the
`{slug:path}` route below the literal routes.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check --fix pr_dashboard/app.py tests/unittest/test_pr_dashboard_views.py
uv run pre-commit run --files pr_dashboard/app.py tests/unittest/test_pr_dashboard_views.py
git add pr_dashboard/app.py pr_dashboard/templates tests/unittest/test_pr_dashboard_views.py
git commit -m "feat(dashboard): add overview, repository, and pull request detail views"
```

---

### Task 8: Usage page and aggregation

**Files:**
- Create: `pr_dashboard/usage.py`
- Modify: `pr_dashboard/app.py` (add the `/usage` route)
- Create: `pr_dashboard/templates/usage.html`
- Create: `docs/docs/tools/dashboard.md`
- Modify: `docs/mkdocs.yml` (add one nav entry)
- Test: `tests/unittest/test_pr_dashboard_usage.py`

**Interfaces:**
- Consumes: `pr_dashboard.store`.
- Produces:
  - `DIMENSIONS: dict[str, str]` mapping a safe dimension name to its column
  - `totals(conn, since: str | None = None) -> dict` with keys `runs`, `ok`, `failed`, `tokens`, `cost`, `unpriced_runs`, `fallback_runs`
  - `by_dimension(conn, dimension: str, since: str | None = None) -> list[dict]` with keys `label`, `runs`, `tokens`, `cost`
  - `daily_tokens(conn, days: int = 30) -> list[dict]` with keys `day`, `tokens`, `cost`

- [ ] **Step 1: Write the failing test**

Create `tests/unittest/test_pr_dashboard_usage.py`:

```python
from decimal import Decimal

import pytest

from pr_dashboard import store, usage


def _seed(conn):
    rows = [
        ("2026-09-08T10:00:00+00:00", "github", "o/a", 1, "review", "flash", 0, 1000, "0.01", "complete", "ok"),
        ("2026-09-09T10:00:00+00:00", "github", "o/a", 2, "improve", "flash", 1, 2000, "0.02", "complete", "ok"),
        ("2026-09-09T11:00:00+00:00", "github", "o/b", 3, "review", "ollama/l3", 0, 500, None, "unavailable", "ok"),
        ("2026-09-10T09:00:00+00:00", "github", "o/b", 4, "review", None, 0, None, None, None, "failed"),
    ]
    for started, provider, slug, number, command, model, fallback, tokens, cost, cost_status, status in rows:
        conn.execute(
            "INSERT INTO runs (started_at, status, provider, repo_slug, pr_number, command, model_used, "
            "fallback_used, total_tokens, total_cost_usd, cost_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (started, status, provider, slug, number, command, model, fallback, tokens, cost, cost_status),
        )


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "usage.db")
    _seed(connection)
    return connection


class TestTotals:
    def test_counts_and_sums(self, conn):
        """Totals count every attempt and sum only priced costs"""
        result = usage.totals(conn)
        assert result["runs"] == 4
        assert result["ok"] == 3
        assert result["failed"] == 1
        assert result["tokens"] == 3500
        assert result["cost"] == Decimal("0.03")

    def test_unpriced_and_fallback_counts(self, conn):
        """Unpriced and fallback runs are reported rather than hidden"""
        result = usage.totals(conn)
        assert result["unpriced_runs"] == 2
        assert result["fallback_runs"] == 1

    def test_since_filter(self, conn):
        """A since bound excludes older runs"""
        result = usage.totals(conn, since="2026-09-09T00:00:00+00:00")
        assert result["runs"] == 3
        assert result["tokens"] == 2500


class TestByDimension:
    def test_by_repo(self, conn):
        """Usage groups by repository, newest cost first"""
        rows = usage.by_dimension(conn, "repo")
        labels = {row["label"]: row for row in rows}
        assert labels["o/a"]["tokens"] == 3000
        assert labels["o/a"]["cost"] == Decimal("0.03")
        assert labels["o/b"]["cost"] == Decimal("0")

    def test_by_model(self, conn):
        """Usage groups by model"""
        rows = usage.by_dimension(conn, "model")
        assert {row["label"] for row in rows} >= {"flash", "ollama/l3"}

    def test_by_command(self, conn):
        """Usage groups by command"""
        rows = usage.by_dimension(conn, "command")
        assert {row["label"] for row in rows} == {"review", "improve"}

    def test_unknown_dimension_is_rejected(self, conn):
        """An unknown dimension raises rather than reaching SQL"""
        with pytest.raises(ValueError):
            usage.by_dimension(conn, "repo_slug; DROP TABLE runs")


class TestDailySeries:
    def test_groups_by_day(self, conn):
        """The daily series has one entry per day with runs"""
        series = usage.daily_tokens(conn, days=30)
        by_day = {row["day"]: row for row in series}
        assert by_day["2026-09-09"]["tokens"] == 2500
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_usage.py -q`

Expected: FAIL — `ImportError: cannot import name 'usage' from 'pr_dashboard'`

- [ ] **Step 3: Implement the aggregation**

Create `pr_dashboard/usage.py`:

```python
"""Aggregate the usage store for the consumption views.

Costs are summed in Python because they are stored as TEXT to preserve Decimal exactness;
at single-user scale that is cheaper than the precision a REAL column would cost. The
dimension whitelist exists because a column name cannot be a bound parameter.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

DIMENSIONS = {
    "repo": "repo_slug",
    "model": "model_used",
    "command": "command",
    "provider": "provider",
}


def _decimal(raw) -> Decimal:
    if raw is None:
        return Decimal("0")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return Decimal("0")


def _since_clause(since: Optional[str]) -> tuple[str, tuple]:
    if since is None:
        return "", ()
    return " WHERE started_at >= ?", (since,)


def totals(conn: sqlite3.Connection, since: Optional[str] = None) -> dict:
    """Return run counts, token sums, and total priced cost over the window."""
    clause, params = _since_clause(since)
    row = conn.execute(
        "SELECT count(*) AS runs, "
        "sum(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok, "
        "sum(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed, "
        "sum(total_tokens) AS tokens, "
        "sum(CASE WHEN total_cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced_runs, "
        "sum(CASE WHEN fallback_used = 1 THEN 1 ELSE 0 END) AS fallback_runs "
        f"FROM runs{clause}",
        params,
    ).fetchone()
    cost_clause = f"{clause} AND" if clause else " WHERE"
    costs = conn.execute(
        f"SELECT total_cost_usd FROM runs{cost_clause} total_cost_usd IS NOT NULL",
        params,
    ).fetchall()
    return {
        "runs": row["runs"] or 0,
        "ok": row["ok"] or 0,
        "failed": row["failed"] or 0,
        "tokens": row["tokens"] or 0,
        "cost": sum((_decimal(r["total_cost_usd"]) for r in costs), Decimal("0")),
        "unpriced_runs": row["unpriced_runs"] or 0,
        "fallback_runs": row["fallback_runs"] or 0,
    }


def by_dimension(conn: sqlite3.Connection, dimension: str, since: Optional[str] = None) -> list[dict]:
    """Group usage by one whitelisted dimension."""
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown usage dimension {dimension!r}; expected one of {', '.join(DIMENSIONS)}")
    column = DIMENSIONS[dimension]
    clause, params = _since_clause(since)
    rows = conn.execute(
        f"SELECT {column} AS label, count(*) AS runs, sum(total_tokens) AS tokens, "
        f"group_concat(total_cost_usd) AS costs FROM runs{clause} "
        f"GROUP BY {column} ORDER BY tokens DESC",
        params,
    ).fetchall()
    result = []
    for row in rows:
        if row["label"] is None:
            continue
        raw_costs = (row["costs"] or "").split(",") if row["costs"] else []
        result.append({
            "label": row["label"],
            "runs": row["runs"],
            "tokens": row["tokens"] or 0,
            "cost": sum((_decimal(value) for value in raw_costs if value), Decimal("0")),
        })
    return result


def daily_tokens(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """Return one entry per day in the window that has at least one run."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT substr(started_at, 1, 10) AS day, sum(total_tokens) AS tokens, "
        "group_concat(total_cost_usd) AS costs FROM runs WHERE started_at >= ? "
        "GROUP BY day ORDER BY day",
        (since,),
    ).fetchall()
    return [
        {
            "day": row["day"],
            "tokens": row["tokens"] or 0,
            "cost": sum(
                (_decimal(value) for value in (row["costs"] or "").split(",") if value), Decimal("0")),
        }
        for row in rows
    ]
```

Note on `daily_tokens`: the seeded test data is dated in the past, so if
`test_groups_by_day` fails on the 30-day window, widen the call in the test to
`days=3650` rather than changing the production default.

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_usage.py -q`

Expected: PASS, 8 tests

- [ ] **Step 5: Create the usage template**

Create `pr_dashboard/templates/usage.html`:

```html
{% extends "base.html" %}
{% block title %}Usage{% endblock %}
{% block content %}
<h1>Usage</h1>
<p>
  {{ totals.runs }} runs — {{ totals.ok }} ok, {{ totals.failed }} failed.
  {{ totals.tokens }} tokens. Cost {{ totals.cost }} USD.
</p>
<p class="muted">
  {{ totals.unpriced_runs }} runs could not be priced (local models, or usage the provider did not report).
  {{ totals.fallback_runs }} runs used a fallback model.
</p>

<canvas id="daily" height="90"></canvas>
<script src="{{ url_for('static', path='/chart.umd.js') }}"></script>
<script>
  const daily = {{ daily | tojson }};
  new Chart(document.getElementById("daily"), {
    type: "line",
    data: {
      labels: daily.map(d => d.day),
      datasets: [{ label: "tokens", data: daily.map(d => d.tokens) }]
    },
    options: { responsive: true, scales: { y: { beginAtZero: true } } }
  });
</script>

{% for group in groups %}
  <h2>By {{ group.name }}</h2>
  <table>
    <thead><tr><th>{{ group.name }}</th><th>Runs</th><th>Tokens</th><th>Cost (USD)</th></tr></thead>
    <tbody>
    {% for row in group.rows %}
      <tr><td>{{ row.label }}</td><td>{{ row.runs }}</td><td>{{ row.tokens }}</td><td>{{ row.cost }}</td></tr>
    {% endfor %}
    </tbody>
  </table>
{% endfor %}
{% endblock %}
```

- [ ] **Step 6: Add the `/usage` route**

In `pr_dashboard/app.py`, add `from pr_dashboard import usage as usage_module` to the
imports and this route inside `create_app`:

```python
    @application.get("/usage", response_class=HTMLResponse)
    def usage_page(request: Request):
        conn = store.connect(application.state.db_path)
        daily = [
            {"day": row["day"], "tokens": row["tokens"], "cost": str(row["cost"])}
            for row in usage_module.daily_tokens(conn, days=30)
        ]
        groups = [
            {"name": name, "rows": usage_module.by_dimension(conn, name)}
            for name in ("repo", "model", "command")
        ]
        return templates.TemplateResponse(request, "usage.html", {
            "totals": usage_module.totals(conn), "daily": daily, "groups": groups,
        })
```

- [ ] **Step 7: Verify the page renders**

Add to `tests/unittest/test_pr_dashboard_usage.py`:

```python
class TestUsagePage:
    def test_renders_with_an_empty_store(self, tmp_path, monkeypatch):
        """The usage page renders before any run has been recorded"""
        from fastapi.testclient import TestClient

        from pr_dashboard import app as app_module

        application = app_module.create_app(
            registry_path=tmp_path / "pr_dashboard.toml", db_path=tmp_path / "empty.db")
        response = TestClient(application).get("/usage")
        assert response.status_code == 200
        assert "0 runs" in response.text
```

Run: `PYTHONPATH=. uv run pytest tests/unittest/test_pr_dashboard_usage.py -q`

Expected: PASS, 9 tests

- [ ] **Step 8: Document the dashboard**

Create `docs/docs/tools/dashboard.md`:

```markdown
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

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `pr_dashboard.record_runs` | `false` | Record one usage row per command run |
```

Add one entry under the `Tools` section of the `nav:` block in `docs/mkdocs.yml`,
matching the existing indentation:

```yaml
      - Dashboard: 'tools/dashboard.md'
```

- [ ] **Step 9: Run the whole unit suite**

```bash
PYTHONPATH=. uv run pytest tests/unittest -q
```

Expected: PASS with no new failures relative to the pre-task baseline. Capture the count
in the commit message.

- [ ] **Step 10: Lint and commit**

```bash
uv run ruff check --fix pr_dashboard/usage.py pr_dashboard/app.py tests/unittest/test_pr_dashboard_usage.py
uv run pre-commit run --files pr_dashboard/usage.py pr_dashboard/app.py docs/docs/tools/dashboard.md docs/mkdocs.yml tests/unittest/test_pr_dashboard_usage.py
git add pr_dashboard/usage.py pr_dashboard/app.py pr_dashboard/templates/usage.html docs/docs/tools/dashboard.md docs/mkdocs.yml tests/unittest/test_pr_dashboard_usage.py
git commit -m "feat(dashboard): add usage aggregation, consumption page, and documentation"
```

---

## Verification before declaring S1 + S2 done

- [ ] `PYTHONPATH=. uv run pytest tests/unittest -q` passes, with the new
  `test_pr_dashboard_*.py` files contributing 66 tests
  (6 store, 10 registry, 10 recorder, 10 comments, 10 providers, 5 app, 6 views, 9 usage).
- [ ] `uv run ruff check pr_dashboard tests/unittest/test_pr_dashboard_*.py` is clean.
- [ ] `awk 'length > 120 {print FILENAME":"NR}' pr_dashboard/*.py pr_agent/agent/pr_agent.py` prints nothing.
- [ ] `git diff main --stat -- pr_agent/` shows changes in exactly two files:
  `pr_agent/agent/pr_agent.py` and `pr_agent/settings/configuration.toml`.
- [ ] `grep -rn "record_runs" pr_agent/settings/configuration.toml` shows the default is `false`.
- [ ] With `pr_dashboard.record_runs = true`, a real `uv run pr-agent --pr_url <url> review`
  produces a row in `~/.pr_dashboard/usage.db` whose `total_tokens` is non-zero, and that
  row is visible on `/usage` and on the pull request's page.
- [ ] `grep -rin "token" pr_dashboard/templates/` shows no template renders a credential
  value — only credential *status* text.

## Out of scope for this plan

S3 run triggering, S4 configuration editing, S5 cross-repo findings index. Each gets its
own spec and plan.
