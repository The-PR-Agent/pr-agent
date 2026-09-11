"""Cross-repository findings index — cache-first, bounded background refresh.

index_snapshot never calls a provider; refresh does the fan-out under per-repo and total
deadlines, single-flighted per repository, with sequential repo processing.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from pr_dashboard import comments as comments_module
from pr_dashboard import providers, registry, store

MAX_INDEXED_PRS_PER_REPO = 20
REPO_DEADLINE_SECONDS = 30
TOTAL_DEADLINE_SECONDS = 90
INDEX_TTL_SECONDS = providers.DEFAULT_TTL_SECONDS
FAILURE_BACKOFF_SECONDS = 60

_VALID_COMMAND_FILTERS = frozenset({"review", "improve"})
_VALID_HAS_FILE_FILTERS = frozenset({"yes", "no"})


@dataclass(frozen=True)
class RepoIndexState:
    status: str
    message: Optional[str] = None


@dataclass(frozen=True)
class FindingRow:
    repo_key: str
    provider: str
    slug: str
    pr_number: int
    pr_title: str
    finding_title: str
    relevant_file: Optional[str]
    line_start: Optional[int]
    line_end: Optional[int]
    command: str


def _cache_key(repo: registry.Repo) -> str:
    return f"findings_index:{repo.key}"


def _db_key(conn) -> str:
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    # An in-memory database reports an empty file. Resolving that would yield the process's
    # cwd, which is a real path that cannot be reopened as this database -- return the empty
    # string so callers can tell the two apart.
    if not path:
        return ""
    return str(Path(path).resolve())


def _read_cache(conn, repo: registry.Repo) -> Optional[dict]:
    row = conn.execute(
        "SELECT payload, expires_at FROM provider_cache WHERE key = ?",
        (_cache_key(repo),),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload"])
    expires_at = datetime.fromisoformat(row["expires_at"])
    payload["_expires_at"] = expires_at
    return payload


def _write_cache(conn, repo: registry.Repo, payload: dict) -> None:
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT OR REPLACE INTO provider_cache (key, fetched_at, expires_at, payload) VALUES (?, ?, ?, ?)",
        (
            _cache_key(repo),
            now.isoformat(),
            (now + timedelta(seconds=INDEX_TTL_SECONDS)).isoformat(),
            json.dumps({key: value for key, value in payload.items() if not key.startswith("_")}),
        ),
    )


class _RepoFlight:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.in_progress = False


class _RefreshTracker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.in_progress = False
        self.completed: set[str] = set()
        self.pending: set[str] = set()


_flights: dict[tuple[str, str], _RepoFlight] = {}
_refresh_trackers: dict[str, _RefreshTracker] = {}
_last_failure: dict[tuple[str, str], float] = {}


def _flight(db_key: str, repo_key: str) -> _RepoFlight:
    key = (db_key, repo_key)
    if key not in _flights:
        _flights[key] = _RepoFlight()
    return _flights[key]


def _tracker(db_key: str) -> _RefreshTracker:
    if db_key not in _refresh_trackers:
        _refresh_trackers[db_key] = _RefreshTracker()
    return _refresh_trackers[db_key]


def _row_from_parts(repo: registry.Repo, pull, finding: comments_module.Finding, command: str) -> FindingRow:
    line_start = line_end = None
    if finding.line_range is not None:
        line_start, line_end = finding.line_range
    return FindingRow(
        repo_key=repo.key,
        provider=repo.provider,
        slug=repo.slug,
        pr_number=pull.number,
        pr_title=pull.title,
        finding_title=finding.title,
        relevant_file=finding.relevant_file,
        line_start=line_start,
        line_end=line_end,
        command=command,
    )


def _serialize_row(row: FindingRow) -> dict:
    return {
        "repo_key": row.repo_key,
        "provider": row.provider,
        "slug": row.slug,
        "pr_number": row.pr_number,
        "pr_title": row.pr_title,
        "finding_title": row.finding_title,
        "relevant_file": row.relevant_file,
        "line_start": row.line_start,
        "line_end": row.line_end,
        "command": row.command,
    }


def _deserialize_row(raw: dict) -> FindingRow:
    return FindingRow(**raw)


def _command_name(kind: comments_module.CommentKind) -> str:
    if kind in (comments_module.CommentKind.REVIEW, comments_module.CommentKind.INCREMENTAL_REVIEW):
        return "review"
    if kind is comments_module.CommentKind.SUGGESTIONS:
        return "improve"
    return kind.value


def _fetch_repo_rows(conn, repo: registry.Repo, deadline: float) -> tuple[list[FindingRow], bool, Optional[str]]:
    """Fetch findings for one repository. Returns (rows, stale, error_message)."""
    if time.monotonic() >= deadline:
        return [], False, None
    try:
        pulls, pulls_stale = providers.list_pull_requests(
            repo, state="open", limit=MAX_INDEXED_PRS_PER_REPO, conn=conn,
        )
    except providers.ProviderError as exc:
        return [], False, str(exc)

    rows: list[FindingRow] = []
    stale = pulls_stale
    for pull in pulls:
        if time.monotonic() >= deadline:
            break
        try:
            review_comments, comments_stale = providers.list_pr_agent_comments(repo, pull.number, conn=conn)
        except providers.ProviderError as exc:
            return rows, stale, str(exc)
        stale = stale or comments_stale
        for comment in review_comments:
            command = _command_name(comment.kind)
            for finding in comments_module.parse_findings(comment.body):
                rows.append(_row_from_parts(repo, pull, finding, command))
    return rows, stale, None


def _in_backoff(db_key: str, repo_key: str) -> bool:
    failed_at = _last_failure.get((db_key, repo_key))
    if failed_at is None:
        return False
    return (time.monotonic() - failed_at) < FAILURE_BACKOFF_SECONDS


def _repo_state(
    conn,
    repo: registry.Repo,
    db_key: str,
    tracker: _RefreshTracker,
    cached: Optional[dict],
) -> RepoIndexState:
    flight = _flight(db_key, repo.key)
    if tracker.in_progress and repo.key in tracker.pending and repo.key not in tracker.completed:
        return RepoIndexState("loading")
    if flight.in_progress:
        return RepoIndexState("loading")
    if cached is None:
        if _in_backoff(db_key, repo.key):
            return RepoIndexState("failed", "provider error (backing off)")
        return RepoIndexState("loading")
    if cached.get("error"):
        return RepoIndexState("failed", cached["error"])
    expires_at = cached.get("_expires_at")
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        return RepoIndexState("stale", "cached data is older than the refresh interval")
    if cached.get("stale"):
        return RepoIndexState("stale", "served from cache after a provider error")
    return RepoIndexState("fresh")


def index_snapshot(conn, repos: list[registry.Repo]) -> dict[str, Any]:
    """Return cached index state and rows. Never calls a provider."""
    db_key = _db_key(conn)
    tracker = _tracker(db_key)
    repo_states: list[dict] = []
    all_rows: list[FindingRow] = []
    any_loading = False
    for repo in repos:
        cached = _read_cache(conn, repo)
        state = _repo_state(conn, repo, db_key, tracker, cached)
        if state.status == "loading":
            any_loading = True
        rows = [_deserialize_row(item) for item in (cached or {}).get("rows", [])]
        all_rows.extend(rows)
        repo_states.append({"repo": repo, "state": state, "rows": rows})
    return {
        "repos": repo_states,
        "rows": all_rows,
        "any_loading": any_loading,
        "max_indexed": MAX_INDEXED_PRS_PER_REPO,
    }


def _refresh_one_repo(conn, repo: registry.Repo, db_key: str, deadline: float) -> None:
    flight = _flight(db_key, repo.key)
    with flight.lock:
        if flight.in_progress:
            return
        if _in_backoff(db_key, repo.key):
            return
        flight.in_progress = True

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        with flight.lock:
            flight.in_progress = False
        return

    result: dict[str, object] = {}
    db_path = _db_key(conn)

    def worker() -> None:
        # A sqlite3.Connection may only be used in the thread that created it, and
        # providers.list_pull_requests takes conn= to reach the TTL cache. Handing the
        # caller's connection to this thread raises ProgrammingError, which would die here
        # unseen and leave the repository cached as fresh with zero rows -- the index would
        # simply always be empty. Open our own connection instead.
        own = None
        try:
            own = store.connect(db_path)
            result["value"] = _fetch_repo_rows(own, repo, deadline)
        except BaseException as exc:  # noqa: BLE001 - a refresh must never kill the thread silently
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if own is not None:
                own.close()
            with flight.lock:
                flight.in_progress = False

    if not db_path:
        # An in-memory store cannot be reopened by path, so there is no second connection to
        # make. Fetch inline; _fetch_repo_rows still honours the deadline between pulls.
        try:
            result["value"] = _fetch_repo_rows(conn, repo, deadline)
        except BaseException as exc:  # noqa: BLE001 - same reason as the worker
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            with flight.lock:
                flight.in_progress = False
    else:
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(remaining)
        if thread.is_alive():
            return
    if "value" not in result:
        _last_failure[(db_key, repo.key)] = time.monotonic()
        _write_cache(conn, repo, {"rows": [], "stale": False,
                                  "error": result.get("error", "the refresh did not complete")})
        return
    rows, stale, error = result["value"]
    if error is not None and not rows:
        _last_failure[(db_key, repo.key)] = time.monotonic()
        _write_cache(conn, repo, {"rows": [], "stale": False, "error": error})
    else:
        _last_failure.pop((db_key, repo.key), None)
        _write_cache(
            conn,
            repo,
            {"rows": [_serialize_row(row) for row in rows], "stale": stale, "error": None},
        )


def refresh(conn, repos: list[registry.Repo]) -> None:
    """Fetch findings for repositories sequentially under per-repo and total deadlines."""
    db_key = _db_key(conn)
    tracker = _tracker(db_key)
    with tracker.lock:
        if tracker.in_progress:
            return
        tracker.in_progress = True
        tracker.completed = set()
        tracker.pending = {repo.key for repo in repos}
    started = time.monotonic()
    try:
        for repo in repos:
            total_remaining = TOTAL_DEADLINE_SECONDS - (time.monotonic() - started)
            if total_remaining <= 0:
                break
            repo_deadline = min(time.monotonic() + REPO_DEADLINE_SECONDS, time.monotonic() + total_remaining)
            _refresh_one_repo(conn, repo, db_key, repo_deadline)
            with tracker.lock:
                tracker.completed.add(repo.key)
                tracker.pending.discard(repo.key)
    finally:
        with tracker.lock:
            tracker.in_progress = False
            tracker.pending.clear()


def filter_rows(
    rows: list[FindingRow],
    *,
    repository: Optional[str] = None,
    command: Optional[str] = None,
    has_file: Optional[str] = None,
    title: Optional[str] = None,
) -> list[FindingRow]:
    """Filter assembled rows in Python. Unknown filter values return nothing."""
    if repository is not None and repository != "":
        rows = [row for row in rows if row.repo_key == repository]
    if command is not None and command != "":
        if command not in _VALID_COMMAND_FILTERS:
            return []
        rows = [row for row in rows if row.command == command]
    if has_file is not None and has_file != "":
        if has_file not in _VALID_HAS_FILE_FILTERS:
            return []
        if has_file == "yes":
            rows = [row for row in rows if row.relevant_file is not None]
        else:
            rows = [row for row in rows if row.relevant_file is None]
    if title is not None and title != "":
        needle = title.casefold()
        rows = [row for row in rows if needle in row.finding_title.casefold()]
    return rows
