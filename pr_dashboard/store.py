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
