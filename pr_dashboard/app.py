"""FastAPI application for the PR-Agent dashboard.

create_app takes explicit paths so tests can run against a temporary registry and
database; the module-level ``app`` uses the real defaults for uvicorn.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pr_dashboard import providers, registry, store

_HERE = Path(__file__).parent


async def _form_values(request: Request) -> dict[str, str]:
    """Parse an urlencoded body without python-multipart.

    Starlette's request.form() asserts on python-multipart even for urlencoded
    bodies (starlette/requests.py:276), and this project must not add runtime
    dependencies.
    """
    body = await request.body()
    # errors="replace": a non-UTF-8 body is malformed input, and it must surface as the
    # page's own validation error rather than a 500 from UnicodeDecodeError.
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

    return application


app = create_app()


def main() -> None:
    """Console entry point for the pr-dashboard script."""
    import uvicorn

    uvicorn.run("pr_dashboard.app:app", host="127.0.0.1", port=8420, reload=False)
