"""CSRF and Origin/Host guards for state-changing dashboard routes.

The dashboard binds to localhost without authentication. Any page the user visits
while it is running can forge a cross-origin POST; these checks are what stop that.
"""
from __future__ import annotations

import secrets
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from starlette.responses import Response

SAFE_HOSTS = {"127.0.0.1", "localhost", "127.0.0.1:8420", "localhost:8420"}
SESSION_COOKIE = "pr_dashboard_session"


def _sessions(request: Request) -> dict[str, str]:
    """Per-app session_id -> csrf_token map (keeps create_app instances isolated)."""
    sessions = getattr(request.app.state, "csrf_sessions", None)
    if sessions is None:
        sessions = {}
        request.app.state.csrf_sessions = sessions
    return sessions


def _session_token(request: Request) -> str:
    """Return the CSRF token for the request's session cookie, or empty if unknown."""
    sid = request.cookies.get(SESSION_COOKIE, "")
    if not sid:
        return ""
    return _sessions(request).get(sid, "")


def csrf_token(request: Request) -> str:
    """Return this session's CSRF token, minting a session on first page load."""
    sessions = _sessions(request)
    sid = request.cookies.get(SESSION_COOKIE, "")
    if sid and sid in sessions:
        return sessions[sid]
    sid = secrets.token_urlsafe(32)
    token = secrets.token_urlsafe(32)
    sessions[sid] = token
    request.state.new_session_id = sid
    return token


def attach_session_cookie(request: Request, response: Response) -> None:
    """Set the httponly session cookie when csrf_token minted a new session."""
    sid = getattr(request.state, "new_session_id", None)
    if sid is not None:
        response.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="strict")


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
