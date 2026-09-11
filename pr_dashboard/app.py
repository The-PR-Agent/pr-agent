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
from pr_dashboard import config_files, config_writer, providers, recorder, redaction, registry, runner, store, websec
from pr_dashboard import usage as usage_module

_HERE = Path(__file__).parent
_REPO_ROOT = _HERE.parent
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


def create_app(
    *,
    registry_path: Optional[Path] = None,
    db_path: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> FastAPI:
    application = FastAPI(title="PR-Agent Dashboard")
    application.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    templates = Jinja2Templates(directory=str(_HERE / "templates"))

    application.state.registry_path = registry_path or registry.DEFAULT_REGISTRY_PATH
    application.state.db_path = db_path or store.DEFAULT_DB_PATH
    application.state.repo_root = repo_root or _REPO_ROOT
    application.state.templates = templates
    application.state.csrf_sessions = {}

    def html(request: Request, name: str, context: dict) -> HTMLResponse:
        context = {**context, "csrf_token": websec.csrf_token(request)}
        response = templates.TemplateResponse(request, name, context)
        websec.attach_session_cookie(request, response)
        return response

    def repo_rows() -> list[dict]:
        return [
            {"repo": repo, "credentials": providers.credential_status(repo.provider)}
            for repo in registry.load(application.state.registry_path)
        ]

    def render_rows(request: Request, error: Optional[str] = None) -> HTMLResponse:
        return html(request, "_repo_rows.html", {"repos": repo_rows(), "error": error})

    @application.get("/repos", response_class=HTMLResponse)
    def repos_page(request: Request):
        return html(request, "repos.html", {"repos": repo_rows(), "error": None})

    @application.post("/repos", response_class=HTMLResponse)
    async def add_repo(request: Request):
        values = await _form_values(request)
        websec.require_safe_request(request, values)
        provider = values.get("provider", "")
        slug = values.get("slug", "")
        try:
            registry.add(registry.Repo(provider=provider, slug=slug), application.state.registry_path)
        except registry.RegistryError as exc:
            return render_rows(request, error=str(exc))
        return render_rows(request)

    @application.post("/repos/{provider}/{slug:path}/delete", response_class=HTMLResponse)
    async def delete_repo(request: Request, provider: str, slug: str):
        values = await _form_values(request)
        websec.require_safe_request(request, values)
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
        # Corrupt TEXT is unpriced (None), never a known free zero — same rule as usage._decimal.
        parsed_costs = [
            amount for amount in (usage_module._decimal(r["total_cost_usd"]) for r in costs)
            if amount is not None
        ]
        total_cost = sum(parsed_costs, Decimal("0")) if parsed_costs else None
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
        return html(request, "overview.html", {"cards": cards, "errors": errors, "stale": stale})

    @application.get("/repos/{provider}/{slug:path}", response_class=HTMLResponse)
    def repo_detail(request: Request, provider: str, slug: str):
        repo = find_repo(provider, slug)
        conn = store.connect(application.state.db_path)
        pulls, error, stale = [], None, False
        try:
            pulls, stale = providers.list_pull_requests(repo, state="open", limit=50, conn=conn)
        except providers.ProviderError as exc:
            error = str(exc)
        return html(request, "repo_detail.html", {"repo": repo, "pulls": pulls, "error": error, "stale": stale})

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
        return html(request, "pr_detail.html", {
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
        return html(request, "usage.html", {
            "totals": usage_module.totals(conn), "daily": daily, "groups": groups,
        })

    def runs_context(conn, error: Optional[str] = None) -> dict:
        return {
            "runs": store.list_ui_runs(conn),
            "repos": registry.load(application.state.registry_path),
            "commands": runner.ALLOWED_COMMANDS,
            "error": error,
        }

    def run_detail_context(conn, token: str, error: Optional[str] = None) -> Optional[dict]:
        run = store.get_ui_run(conn, token)
        if run is None:
            return None
        accounting = store.run_for_token(conn, token)
        # The log is withheld, never shown raw, when the secret inventory cannot be read.
        # runner.tail_log deliberately lets RedactionUnavailable propagate; the route is where
        # it becomes a message on the page rather than a 500.
        try:
            log_tail, log_unavailable = runner.tail_log(run["log_path"]), False
        except redaction.RedactionUnavailable as exc:
            log_tail, log_unavailable = "", str(exc) or "redaction is unavailable"
        finished = run["finished_at"] is not None
        return {
            "run": run,
            "accounting": accounting,
            "accounting_cost": store.run_cost(accounting) if accounting is not None else None,
            "recording_enabled": recorder.recording_enabled(),
            "log_tail": log_tail,
            "log_unavailable": log_unavailable,
            "timeout_seconds": runner.RUN_TIMEOUT_SECONDS,
            "timed_out": (
                run["status"] == "failed" and finished
                and runner.run_duration_seconds(run) >= runner.RUN_TIMEOUT_SECONDS
            ),
            "error": error,
        }

    @application.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        conn = store.connect(application.state.db_path)
        runner.reap(conn)
        return html(request, "runs.html", runs_context(conn))

    @application.post("/runs", response_class=HTMLResponse)
    async def start_run_route(request: Request):
        values = await _form_values(request)
        websec.require_safe_request(request, values)
        conn = store.connect(application.state.db_path)
        provider = values.get("provider", "")
        slug = values.get("slug", "")
        command = values.get("command", "")
        question = values.get("question") or None
        raw_number = values.get("number", "")
        try:
            number = int(raw_number)
        except ValueError:
            return html(request, "runs.html", runs_context(conn, error=f"invalid pull request number {raw_number!r}"))
        try:
            repo = runner.validate_target(application.state.registry_path, provider, slug, number)
            token = runner.launch(conn, repo=repo, number=number, command=command, question=question)
        except runner.RunError as exc:
            return html(request, "runs.html", runs_context(conn, error=str(exc)))
        return html(request, "run_detail.html", run_detail_context(conn, token))

    @application.get("/runs/{token}", response_class=HTMLResponse)
    def run_detail_page(request: Request, token: str):
        conn = store.connect(application.state.db_path)
        runner.reap(conn)
        context = run_detail_context(conn, token)
        if context is None:
            raise HTTPException(status_code=404, detail=f"unknown run {token!r}")
        return html(request, "run_detail.html", context)

    @application.post("/runs/{token}/cancel", response_class=HTMLResponse)
    async def cancel_run_route(request: Request, token: str):
        values = await _form_values(request)
        websec.require_safe_request(request, values)
        conn = store.connect(application.state.db_path)
        error = None
        try:
            runner.cancel(conn, token)
        except runner.RunError as exc:
            error = str(exc)
        context = run_detail_context(conn, token, error=error)
        if context is None:
            raise HTTPException(status_code=404, detail=f"unknown run {token!r}")
        return html(request, "run_detail.html", context)

    _CONFIG_GROUP_ORDER = ("repository", "defaults", "prompts")
    # Both, everywhere: config_writer raises ConfigWriteError, but the safety check it calls
    # raises ConfigFileError, and a discovered entry that is a symlink or a FIFO reaches it
    # from ordinary user input. Catching only the first makes that case a 500.
    _CONFIG_ERRORS = (config_writer.ConfigWriteError, config_files.ConfigFileError)

    def discover_config_files():
        return config_files.discover(application.state.repo_root)

    def grouped_config_files(files: list[config_files.ConfigFile]) -> list[tuple[str, list[config_files.ConfigFile]]]:
        buckets: dict[str, list[config_files.ConfigFile]] = {name: [] for name in _CONFIG_GROUP_ORDER}
        for entry in files:
            buckets.setdefault(entry.group, []).append(entry)
        return [(name, buckets[name]) for name in _CONFIG_GROUP_ORDER if buckets[name]]

    def config_edit_context(
        config_file: Optional[config_files.ConfigFile],
        content: str,
        *,
        error: Optional[str] = None,
        diff: Optional[str] = None,
        preview_token: Optional[str] = None,
        applied_backup: Optional[Path] = None,
    ) -> dict:
        return {
            "config_file": config_file,
            "content": content,
            "error": error,
            "diff": diff,
            "preview_token": preview_token,
            "applied_backup": applied_backup,
        }

    def read_config_content(path: Path) -> str:
        # Safety-check BEFORE reading, not only before writing. Opening a FIFO for read blocks
        # until someone writes to it, so a non-regular file in the editable set would hang the
        # request thread forever rather than raising -- a worse failure than a 500.
        config_files.assert_safe_target(path)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    @application.get("/config", response_class=HTMLResponse)
    def config_list_page(request: Request):
        files = discover_config_files()
        return html(request, "config_list.html", {"groups": grouped_config_files(files), "error": None})

    @application.get("/config/backups", response_class=HTMLResponse)
    def config_backups_page(request: Request):
        return html(request, "config_backups.html", {"backups": config_writer.list_backups(), "error": None})

    @application.post("/config/backups/{backup_id}/restore", response_class=HTMLResponse)
    async def restore_config_backup(request: Request, backup_id: str):
        values = await _form_values(request)
        websec.require_safe_request(request, values)
        error = None
        try:
            config_writer.restore(backup_id)
        except _CONFIG_ERRORS as exc:
            error = str(exc)
        return html(request, "config_backups.html", {"backups": config_writer.list_backups(), "error": error})

    @application.get("/config/{index}", response_class=HTMLResponse)
    def config_edit_page(request: Request, index: int):
        try:
            discover_config_files()
            config_file = config_files.resolve(index)
        except config_files.ConfigFileError as exc:
            return html(request, "config_edit.html", config_edit_context(None, "", error=str(exc)))
        try:
            content = read_config_content(config_file.path)
        except config_files.ConfigFileError as exc:
            return html(request, "config_edit.html", config_edit_context(config_file, "", error=str(exc)))
        return html(request, "config_edit.html", config_edit_context(config_file, content))

    @application.post("/config/{index}/preview", response_class=HTMLResponse)
    async def config_preview_route(request: Request, index: int):
        values = await _form_values(request)
        websec.require_safe_request(request, values)
        submitted = values.get("content", "")
        try:
            discover_config_files()
            config_file = config_files.resolve(index)
        except config_files.ConfigFileError as exc:
            return html(request, "config_edit.html", config_edit_context(None, submitted, error=str(exc)))
        try:
            diff, token = config_writer.make_preview(config_file, submitted)
        except _CONFIG_ERRORS as exc:
            return html(
                request,
                "config_edit.html",
                config_edit_context(config_file, submitted, error=str(exc)),
            )
        return html(
            request,
            "config_edit.html",
            config_edit_context(config_file, submitted, diff=diff, preview_token=token.value),
        )

    @application.post("/config/{index}", response_class=HTMLResponse)
    async def config_apply_route(request: Request, index: int):
        values = await _form_values(request)
        websec.require_safe_request(request, values)
        submitted = values.get("content", "")
        token_value = values.get("preview_token", "")
        try:
            discover_config_files()
            config_file = config_files.resolve(index)
        except config_files.ConfigFileError as exc:
            return html(request, "config_edit.html", config_edit_context(None, submitted, error=str(exc)))
        try:
            backup_path = config_writer.apply(token_value, submitted)
        except _CONFIG_ERRORS as exc:
            return html(
                request,
                "config_edit.html",
                config_edit_context(config_file, submitted, error=str(exc)),
            )
        return html(
            request,
            "config_edit.html",
            config_edit_context(
                config_file, read_config_content(config_file.path), applied_backup=backup_path,
            ),
        )

    return application


app = create_app()


def main() -> None:
    """Console entry point for the pr-dashboard script."""
    import uvicorn

    uvicorn.run("pr_dashboard.app:app", host="127.0.0.1", port=8420, reload=False)
