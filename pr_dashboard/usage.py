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
