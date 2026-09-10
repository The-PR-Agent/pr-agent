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
