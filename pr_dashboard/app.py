"""FastAPI application for the PR-Agent dashboard.

create_app takes explicit paths so tests can run against a temporary registry and
database; the module-level ``app`` uses the real defaults for uvicorn.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pr_dashboard import comments as comments_module
from pr_dashboard import providers, registry, store
from pr_dashboard import usage as usage_module

_HERE = Path(__file__).parent
_OPEN_PRS_DISPLAY_LIMIT = 50


async def _form_values(request: Request) -> dict[str, str]:
    """Parse an urlencoded body without python-multipart.

    Starlette's request.form() asserts on python-multipart even for urlencoded
    bodies (starlette/requests.py:276), and this project must not add runtime
    dependencies.
    """
    body = await request.body()
    # errors="replace" so a non-UTF-8 body cannot 500 on UnicodeDecodeError. Replacement
    # characters do not satisfy SLUG_PATTERN, so a mangled slug is still rejected by
    # validation rather than mangled through; a bad byte in a field nobody reads is inert.
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items()}


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
        return templates.TemplateResponse(request, "_repo_rows.html", {"repos": repo_rows(), "error": error})

    @application.get("/repos", response_class=HTMLResponse)
    def repos_page(request: Request):
        return templates.TemplateResponse(request, "repos.html", {"repos": repo_rows(), "error": None})

    @application.post("/repos", response_class=HTMLResponse)
    async def add_repo(request: Request):
        values = await _form_values(request)
        provider = values.get("provider", "")
        slug = values.get("slug", "")
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

    def find_repo(provider: str, slug: str) -> registry.Repo:
        # Defense in depth: never let an unvalidated provider/slug reach a provider API URL
        # or the store just because it happened not to match anything in the registry loop
        # below. registry.load() already drops invalid entries, but this route must not rely
        # on that alone.
        if provider not in registry.SUPPORTED_PROVIDERS or not registry.SLUG_PATTERN.match(slug):
            raise HTTPException(status_code=404, detail=f"{provider}:{slug} is not registered")
        for repo in registry.load(application.state.registry_path):
            if repo.provider == provider and repo.slug == slug:
                return repo
        raise HTTPException(status_code=404, detail=f"{provider}:{slug} is not registered")

    def repo_summary(conn, repo: registry.Repo) -> dict:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        # runs.repo_slug is parsed from a PR URL (see recorder.parse_pr_url), while the
        # registry stores whatever the user typed when they registered the repository -- so
        # a repo registered as "Owner/Repo" must still match its own "owner/repo" runs.
        row = conn.execute(
            "SELECT sum(total_tokens) AS tokens, max(started_at) AS last_run "
            "FROM runs WHERE provider = ? AND lower(repo_slug) = lower(?) AND started_at >= ?",
            (repo.provider, repo.slug, since),
        ).fetchone()
        costs = conn.execute(
            "SELECT total_cost_usd FROM runs "
            "WHERE provider = ? AND lower(repo_slug) = lower(?) AND started_at >= ? AND total_cost_usd IS NOT NULL",
            (repo.provider, repo.slug, since),
        ).fetchall()
        # Guarded the same way usage._decimal is: one corrupt cost row must not 500 the page.
        total_cost = (
            sum((usage_module._decimal(r["total_cost_usd"]) for r in costs), Decimal("0"))
            if costs else None
        )
        reviewed_row = conn.execute(
            "SELECT count(DISTINCT pr_number) AS reviewed FROM runs "
            "WHERE provider = ? AND lower(repo_slug) = lower(?) AND started_at >= ? AND pr_number IS NOT NULL",
            (repo.provider, repo.slug, since),
        ).fetchone()
        return {
            "repo": repo,
            "open_prs": None,
            "reviewed_prs": reviewed_row["reviewed"],
            "last_run": row["last_run"],
            "tokens_7d": row["tokens"],
            "cost_7d": total_cost,
        }

    @application.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        conn = store.connect(application.state.db_path)
        cards, errors, stale = [], [], False
        for repo in registry.load(application.state.registry_path):
            card = repo_summary(conn, repo)
            try:
                # Fetch one more than the display limit so a repo with more open PRs than fit
                # can be shown as "50+" instead of silently reporting the truncated count (50)
                # as if it were the whole truth.
                pulls, repo_stale = providers.list_pull_requests(
                    repo, state="open", limit=_OPEN_PRS_DISPLAY_LIMIT + 1, conn=conn)
                count = len(pulls)
                card["open_prs"] = f"{_OPEN_PRS_DISPLAY_LIMIT}+" if count > _OPEN_PRS_DISPLAY_LIMIT else count
                stale = stale or repo_stale
            except providers.ProviderError as exc:
                # Attributed per repo: with several repos registered, a single collapsed
                # message would hide which one failed and silently swallow the rest.
                errors.append(f"{repo.key}: {exc}")
            cards.append(card)
        return templates.TemplateResponse(
            request, "overview.html", {"cards": cards, "errors": errors, "stale": stale})

    @application.get("/repos/{provider}/{slug:path}", response_class=HTMLResponse)
    def repo_detail(request: Request, provider: str, slug: str):
        repo = find_repo(provider, slug)
        conn = store.connect(application.state.db_path)
        pulls, error, stale = [], None, False
        try:
            pulls, stale = providers.list_pull_requests(repo, state="open", limit=50, conn=conn)
        except providers.ProviderError as exc:
            error = str(exc)
        return templates.TemplateResponse(
            request, "repo_detail.html", {"repo": repo, "pulls": pulls, "error": error, "stale": stale})

    @application.get("/pr/{provider}/{slug:path}/{number}", response_class=HTMLResponse)
    def pr_detail(request: Request, provider: str, slug: str, number: int):
        repo = find_repo(provider, slug)
        conn = store.connect(application.state.db_path)
        review_comments, findings, error, stale = [], [], None, False
        try:
            review_comments, stale = providers.list_pr_agent_comments(repo, number, conn=conn)
            for comment in review_comments:
                findings.extend(comments_module.parse_findings(comment.body))
        except providers.ProviderError as exc:
            error = str(exc)
        runs = conn.execute(
            "SELECT * FROM runs WHERE provider = ? AND repo_slug = ? AND pr_number = ? "
            "ORDER BY started_at DESC",
            (repo.provider, repo.slug, number),
        ).fetchall()
        return templates.TemplateResponse(request, "pr_detail.html", {
            "repo": repo, "number": number, "findings": findings,
            "review_comments": review_comments, "runs": runs, "error": error, "stale": stale,
        })

    @application.get("/usage", response_class=HTMLResponse)
    def usage_page(request: Request, dimension: Optional[str] = None):
        # dimension is user-controlled input (a query parameter); it must be checked against
        # the DIMENSIONS whitelist here rather than trusted through to by_dimension, which
        # interpolates it into a SQL column name.
        if dimension is not None and dimension not in usage_module.DIMENSIONS:
            raise HTTPException(status_code=400, detail=f"unknown usage dimension {dimension!r}")
        conn = store.connect(application.state.db_path)
        daily = [
            {"day": row["day"], "tokens": row["tokens"],
             "cost": str(row["cost"]) if row["cost"] is not None else None}
            for row in usage_module.daily_tokens(conn, days=30)
        ]
        dimension_names = (dimension,) if dimension else ("repo", "model", "command")
        groups = [
            {"name": name, "rows": usage_module.by_dimension(conn, name)}
            for name in dimension_names
        ]
        return templates.TemplateResponse(request, "usage.html", {
            "totals": usage_module.totals(conn), "daily": daily, "groups": groups,
        })

    return application


app = create_app()


def main() -> None:
    """Console entry point for the pr-dashboard script."""
    import uvicorn

    uvicorn.run("pr_dashboard.app:app", host="127.0.0.1", port=8420, reload=False)
