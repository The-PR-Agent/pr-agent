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

LOCK_TIMEOUT_SECONDS = 5.0


def _default_db_path() -> Path:
    """Compute the default db path lazily, never at import time.

    Path.home() raises RuntimeError (not ImportError) when HOME is unset and there is no
    passwd entry for the current user, and the guard in pr_agent/agent/pr_agent.py around
    importing pr_dashboard.recorder only catches ImportError -- so a module-level
    ``Path.home()`` here would take down every pr-agent command in that environment, not
    just the dashboard.
    """
    return Path.home() / ".pr_dashboard" / "usage.db"


def __getattr__(name: str):
    # PEP 562: keeps `store.DEFAULT_DB_PATH` working as a module attribute for existing
    # callers (app.py's create_app, tests) while deferring the Path.home() call until
    # something actually asks for it, instead of at import time.
    if name == "DEFAULT_DB_PATH":
        return _default_db_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

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
    """
    CREATE TABLE IF NOT EXISTS ui_runs (
        token        TEXT PRIMARY KEY,
        provider     TEXT NOT NULL,
        repo_slug    TEXT NOT NULL,
        pr_number    INTEGER,
        pr_url       TEXT NOT NULL,
        command      TEXT NOT NULL,
        status       TEXT NOT NULL,
        pid          INTEGER,
        exit_code    INTEGER,
        log_path     TEXT NOT NULL,
        started_at   TEXT NOT NULL,
        finished_at  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ui_runs_started ON ui_runs(started_at DESC)",
)

_FINISH_WITH_USAGE = (
    "UPDATE runs SET status = ?, finished_at = ?, error_text = ?, model_used = ?, "
    "fallback_used = ?, prompt_tokens = ?, completion_tokens = ?, total_tokens = ?, "
    "num_ai_calls = ?, known_cost_call_count = ?, cost_status = ?, total_cost_usd = ?, "
    "duration_seconds = ? WHERE id = ?"
)


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open the store, creating the file and schema when absent."""
    if db_path is None:
        db_path = _default_db_path()
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
    """Apply the schema. Idempotent: IF NOT EXISTS statements plus a guarded column add."""
    for statement in _SCHEMA:
        conn.execute(statement)
    # ALTER TABLE ADD COLUMN is not idempotent — inspect columns rather than catching
    # the duplicate-column error, so a genuine failure is not swallowed.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "dashboard_token" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN dashboard_token TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS runs_dashboard_token ON runs(dashboard_token)")


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
        # Populated now for a future accurate per-model cost breakdown, but deliberately not
        # read anywhere yet -- usage.by_dimension(conn, "model") still groups by model_used
        # (the last model a run used), so a fallback run's entire cost is attributed to its
        # final model rather than split across run_model_costs. Do not delete this as dead
        # code; the honest-for-now mitigation is the fallback_runs note on the usage page.
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


def start_ui_run(conn: sqlite3.Connection, *, token: str, provider: str, repo_slug: str,
                 pr_number: Optional[int], pr_url: str, command: str, log_path: str,
                 started_at: str) -> None:
    """Record a dashboard-launched invocation before the child process is spawned."""
    conn.execute(
        "INSERT INTO ui_runs (token, provider, repo_slug, pr_number, pr_url, command, status, "
        "log_path, started_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
        (token, provider, repo_slug, pr_number, pr_url, command, log_path, started_at),
    )


def finish_ui_run(conn: sqlite3.Connection, *, token: str, status: str,
                  exit_code: Optional[int], finished_at: str) -> None:
    """Mark a dashboard invocation terminal with its exit code and finish time."""
    conn.execute(
        "UPDATE ui_runs SET status = ?, exit_code = ?, finished_at = ? WHERE token = ?",
        (status, exit_code, finished_at, token),
    )


def get_ui_run(conn: sqlite3.Connection, token: str) -> Optional[sqlite3.Row]:
    """Return one ui_runs row by token, or None when unknown."""
    return conn.execute("SELECT * FROM ui_runs WHERE token = ?", (token,)).fetchone()


def list_ui_runs(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Return recent dashboard invocations, newest first."""
    return conn.execute(
        "SELECT * FROM ui_runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()


def run_for_token(conn: sqlite3.Connection, token: str) -> Optional[sqlite3.Row]:
    """Return the accounting runs row joined by dashboard_token, if any."""
    return conn.execute(
        "SELECT * FROM runs WHERE dashboard_token = ?", (token,)
    ).fetchone()
