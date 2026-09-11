"""Launch PR-Agent CLI runs from the dashboard.

A run is a subprocess with a hardened argv and a minimal environment. The PR URL on the
command line is always rebuilt from a validated registry entry — never taken from the
request — so a validation gap cannot put attacker-controlled text on the argv.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from pr_agent.algo.utils import encode_user_text_arg
from pr_dashboard import store
from pr_dashboard.registry import SLUG_PATTERN, SUPPORTED_PROVIDERS, Repo, load

ALLOWED_COMMANDS = ("review", "improve", "describe", "ask")
MAX_CONCURRENT_RUNS = 2
RUN_TIMEOUT_SECONDS = 1800

PROVIDER_HOSTS = {
    "github": "github.com",
    "bitbucket": "bitbucket.org",
}
# Path segment between slug and PR number; kept beside PROVIDER_HOSTS for one lookup site.
_PR_PATH_SEGMENT = {
    "github": "pull",
    "bitbucket": "pull-requests",
}

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Base process environment. Credentials the child needs are copied from a fixed allowlist
# of known provider/model variables — never the ambient environment wholesale.
_BASE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH")
_CREDENTIAL_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_KEY",
    "OPENAI__KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC__KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB__USER_TOKEN",
    "BITBUCKET__BEARER_TOKEN",
    "BITBUCKET__BASIC_TOKEN",
    "GITLAB__PERSONAL_ACCESS_TOKEN",
    "GOOGLE_AI_STUDIO__GEMINI_API_KEY",
    "COHERE__KEY",
    "GROQ__KEY",
    "XAI__KEY",
    "HUGGINGFACE__KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_DEFAULT_REGION",
    "AWS_REGION",
)


class RunError(ValueError):
    """Raised when a run cannot be launched safely."""


def pr_url_for(repo: Repo, number: int) -> str:
    """Build the canonical PR URL from a registry entry and a validated number."""
    host = PROVIDER_HOSTS.get(repo.provider)
    segment = _PR_PATH_SEGMENT.get(repo.provider)
    if host is None or segment is None:
        raise RunError(f"unsupported provider {repo.provider!r}")
    return f"https://{host}/{repo.slug}/{segment}/{number}"


def assert_safe_pr_url(url: str, *, provider: str, slug: str, number: int) -> None:
    """Refuse a PR URL that fails the authority rules in spec section A."""
    # Exact host equality (not suffix matching): github.com.evil.test must fail.
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise RunError(f"PR URL must be https, got {parsed.scheme!r}")
    expected_host = PROVIDER_HOSTS.get(provider)
    if expected_host is None or parsed.hostname != expected_host:
        raise RunError(f"PR URL host {parsed.hostname!r} is not the configured host for {provider}")
    if parsed.username is not None or parsed.password is not None:
        raise RunError("PR URL must not contain userinfo")
    if parsed.query or parsed.fragment:
        raise RunError("PR URL must not contain a query or fragment")
    path = parsed.path or ""
    segments = [part for part in path.split("/") if part != ""]
    if any(part in {".", ".."} for part in segments):
        raise RunError("PR URL path must be canonical")
    segment = _PR_PATH_SEGMENT.get(provider)
    if segment is None:
        raise RunError(f"unsupported provider {provider!r}")
    expected = f"/{slug}/{segment}/{number}"
    if path.rstrip("/") != expected.rstrip("/"):
        raise RunError(f"PR URL path {path!r} does not match the registry target")


def build_argv(repo: Repo, number: int, command: str, question: str | None = None) -> list[str]:
    """Build the child argv. Never a shell string, and never caller text in a flag position."""
    if command not in ALLOWED_COMMANDS:
        raise RunError(f"unsupported command {command!r}; expected one of {', '.join(ALLOWED_COMMANDS)}")
    # The URL is rebuilt from the validated registry entry, never taken from the request,
    # so even a validation gap cannot put attacker-controlled text on the command line.
    argv = ["uv", "run", "pr-agent", "--pr_url", pr_url_for(repo, number), command]
    if command == "ask":
        if not question:
            raise RunError("ask requires a question")
        # encode_user_text_arg, not a bare argv element: pr_agent/cli.py turns any argument
        # beginning "--" into a settings override, so an unencoded question of
        # --pr_questions.extra_instructions=... would silently reconfigure the run.
        # pr_agent/servers/azuredevops_server_webhook.py:137 encodes for the same reason.
        argv.append(encode_user_text_arg(question))
    elif question:
        raise RunError(f"{command} does not take free text")
    return argv


def build_env() -> dict[str, str]:
    """Return a minimal child environment. Never inherit the ambient environment wholesale."""
    env: dict[str, str] = {}
    for key in (*_BASE_ENV_KEYS, *_CREDENTIAL_ENV_KEYS):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


def validate_target(registry_path, provider: str, slug: str, number: int) -> Repo:
    """Return the registry Repo for a form target after authority and membership checks."""
    if provider not in SUPPORTED_PROVIDERS:
        raise RunError(
            f"unsupported provider {provider!r}; expected one of {', '.join(SUPPORTED_PROVIDERS)}"
        )
    if not SLUG_PATTERN.match(slug):
        raise RunError(f"invalid repository slug {slug!r}; expected owner/name")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise RunError(f"invalid pull request number {number!r}")
    repos = load(registry_path)
    match = next((repo for repo in repos if repo.provider == provider and repo.slug == slug), None)
    if match is None:
        raise RunError(f"{slug} is not registered for {provider}")
    # Rebuild and re-check so even a future change to pr_url_for cannot skip the rules.
    assert_safe_pr_url(pr_url_for(match, number), provider=provider, slug=slug, number=number)
    return match


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _active_run_count(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM ui_runs WHERE status IN ('queued', 'running')"
    ).fetchone()
    return int(row["n"])


def _default_log_dir() -> Path:
    return Path.home() / ".pr_dashboard" / "logs"


def launch(conn, *, repo: Repo, number: int, command: str, question: str | None = None,
           log_dir: Path | str | None = None, cwd: Path | str | None = None) -> str:
    """Queue a ui_runs row, then spawn the child. Returns the run token."""
    if _active_run_count(conn) >= MAX_CONCURRENT_RUNS:
        raise RunError(f"at most {MAX_CONCURRENT_RUNS} concurrent runs are allowed")

    argv = build_argv(repo, number, command, question)
    token = str(uuid.uuid4())
    logs = Path(log_dir) if log_dir is not None else _default_log_dir()
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{token}.log"
    # 0600 before any content is written: the raw log can contain secrets.
    fd = os.open(str(log_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.close(fd)

    # Write queued before Popen so a crash between decision and spawn leaves an audit row.
    store.start_ui_run(
        conn,
        token=token,
        provider=repo.provider,
        repo_slug=repo.slug,
        pr_number=number,
        pr_url=pr_url_for(repo, number),
        command=command,
        log_path=str(log_path),
        started_at=_now(),
    )

    env = build_env()
    env["PR_DASHBOARD_RUN_TOKEN"] = token
    workdir = Path(cwd) if cwd is not None else _REPO_ROOT

    log_file = open(log_path, "ab")  # noqa: SIM115 - closed after Popen duplicates the fd
    try:
        # shell=False and an argv list: never interpolate into a shell command string.
        proc = subprocess.Popen(
            argv,
            shell=False,
            env=env,
            cwd=str(workdir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    finally:
        log_file.close()

    conn.execute(
        "UPDATE ui_runs SET status = ?, pid = ? WHERE token = ?",
        ("running", proc.pid, token),
    )
    return token
