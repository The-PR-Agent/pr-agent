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


def _maybe_cached(conn, key: str, fetch) -> tuple[object, bool]:
    """Apply the provider cache when a connection is available, else fetch directly."""
    if conn is None:
        return fetch(), False
    return cached(conn, key, DEFAULT_TTL_SECONDS, fetch)


def list_pull_requests(repo: registry.Repo, state: str = "open", limit: int = 50,
                       conn=None) -> tuple[list[PullRequestSummary], bool]:
    """Return (pull requests, is_stale), newest update first."""
    if repo.provider == "github":
        def fetch():
            return _fetch_github_pull_requests(repo, state, limit)
    elif repo.provider == "bitbucket":
        def fetch():
            return _fetch_bitbucket_pull_requests(repo, state, limit)
    else:
        raise ProviderError(f"unsupported provider {repo.provider!r}")

    raw, stale = _maybe_cached(conn, f"{repo.key}:pulls:{state}:{limit}", fetch)
    return [PullRequestSummary(**item) for item in raw], stale


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


def list_pr_agent_comments(repo: registry.Repo, number: int, conn=None) -> tuple[list[ReviewComment], bool]:
    """Return (PR-Agent's comments on a pull request, is_stale)."""
    if repo.provider == "github":
        def fetch():
            return _fetch_github_issue_comments(repo, number)
    elif repo.provider == "bitbucket":
        def fetch():
            return _fetch_bitbucket_comments(repo, number)
    else:
        raise ProviderError(f"unsupported provider {repo.provider!r}")

    raw, stale = _maybe_cached(conn, f"{repo.key}:comments:{number}", fetch)
    result = []
    for item in raw:
        kind = comments_module.classify(item["body"])
        if kind is comments_module.CommentKind.OTHER:
            continue
        result.append(ReviewComment(
            kind=kind, body=item["body"], created_at=item["created_at"], url=item["html_url"]))
    return result, stale
