import os
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import pytest

from pr_agent.cli import set_parser
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import utils as git_utils
from pr_agent.git_providers.utils import (
    _apply_settings_from_file,
    _resolve_extra_config_to_file,
    apply_repo_settings,
)


SAMPLE_TOML = b'[config]\nmodel = "claude-sonnet-4-6"\n'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _CapturingHandler(BaseHTTPRequestHandler):
    """Tiny HTTP handler that serves a configurable body and records request headers."""

    body = SAMPLE_TOML
    expected_path = "/shared.pr_agent.toml"
    require_header = None  # (name, value) tuple if auth must be present
    captured_headers = {}

    def do_GET(self):  # noqa: N802 - http.server API
        if urlparse(self.path).path != self.expected_path:
            self.send_response(404)
            self.end_headers()
            return
        if self.require_header:
            name, value = self.require_header
            if self.headers.get(name) != value:
                self.send_response(401)
                self.end_headers()
                return
        type(self).captured_headers = {k: v for k, v in self.headers.items()}
        self.send_response(200)
        self.send_header("Content-Type", "application/toml")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args, **_kwargs):  # silence test output
        return


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def http_server():
    """Spin up _CapturingHandler on a free port for the duration of one test."""
    # Reset handler-level state so tests don't pollute each other.
    _CapturingHandler.body = SAMPLE_TOML
    _CapturingHandler.expected_path = "/shared.pr_agent.toml"
    _CapturingHandler.require_header = None
    _CapturingHandler.captured_headers = {}

    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _CapturingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def toml_on_disk():
    fd, path = tempfile.mkstemp(suffix=".toml")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(SAMPLE_TOML)
        yield path
    finally:
        if os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------------------
# Resolver tests
# ---------------------------------------------------------------------------

def test_resolve_returns_bare_local_path_without_tempfile(toml_on_disk):
    path, is_temp = _resolve_extra_config_to_file(toml_on_disk)
    assert path == toml_on_disk
    assert is_temp is False


def test_resolve_accepts_file_url_scheme(toml_on_disk):
    path, is_temp = _resolve_extra_config_to_file(f"file://{toml_on_disk}")
    assert path == toml_on_disk
    assert is_temp is False


def test_resolve_returns_none_for_missing_local_file():
    path, is_temp = _resolve_extra_config_to_file("/definitely/does/not/exist.toml")
    assert path is None
    assert is_temp is False


def test_resolve_returns_none_for_empty_source():
    path, is_temp = _resolve_extra_config_to_file("")
    assert path is None
    assert is_temp is False


def test_resolve_rejects_unsupported_scheme():
    path, is_temp = _resolve_extra_config_to_file("ftp://example.com/shared.toml")
    assert path is None
    assert is_temp is False


def test_resolve_fetches_http_url_into_tempfile(http_server):
    url = f"{http_server}/shared.pr_agent.toml"
    path, is_temp = _resolve_extra_config_to_file(url)
    try:
        assert is_temp is True
        assert path and os.path.isfile(path)
        with open(path, "rb") as f:
            assert f.read() == SAMPLE_TOML
        # The fetched tempfile should be a .toml so the downstream loader accepts it
        assert path.endswith(".toml")
    finally:
        if path and os.path.exists(path):
            os.remove(path)


def test_resolve_injects_auth_header_from_env(http_server, monkeypatch):
    _CapturingHandler.require_header = ("Private-Token", "glpat-xxxx")
    monkeypatch.setenv("PR_AGENT_EXTRA_CONFIG_AUTH_HEADER", "PRIVATE-TOKEN: glpat-xxxx")

    url = f"{http_server}/shared.pr_agent.toml"
    path, is_temp = _resolve_extra_config_to_file(url)
    try:
        assert path is not None, "fetch should succeed when auth header is provided"
        assert is_temp is True
        # http.server normalizes header names to title-case
        assert _CapturingHandler.captured_headers.get("Private-Token") == "glpat-xxxx"
    finally:
        if path and os.path.exists(path):
            os.remove(path)


def test_resolve_returns_none_when_auth_header_missing(http_server, monkeypatch):
    _CapturingHandler.require_header = ("Private-Token", "glpat-xxxx")
    monkeypatch.delenv("PR_AGENT_EXTRA_CONFIG_AUTH_HEADER", raising=False)

    url = f"{http_server}/shared.pr_agent.toml"
    path, is_temp = _resolve_extra_config_to_file(url)
    # 401 from the server should be swallowed and return (None, False)
    assert path is None
    assert is_temp is False


def test_resolve_returns_none_on_http_error(http_server):
    # Path mismatch -> 404 from our handler
    url = f"{http_server}/wrong-path.toml"
    path, is_temp = _resolve_extra_config_to_file(url)
    assert path is None
    assert is_temp is False


def test_resolve_rejects_oversized_response(http_server):
    # The resolver caps at 1 MB. Serve 2 MB and confirm it's rejected.
    _CapturingHandler.body = b"x" * (2 * 1024 * 1024)
    url = f"{http_server}/shared.pr_agent.toml"
    path, is_temp = _resolve_extra_config_to_file(url)
    assert path is None
    assert is_temp is False


def test_resolve_malformed_auth_header_is_ignored(http_server, monkeypatch):
    # Header without ':' should be silently dropped, request proceeds without it
    _CapturingHandler.require_header = None
    monkeypatch.setenv("PR_AGENT_EXTRA_CONFIG_AUTH_HEADER", "no-colon-here")

    url = f"{http_server}/shared.pr_agent.toml"
    path, is_temp = _resolve_extra_config_to_file(url)
    try:
        assert path is not None
        assert is_temp is True
    finally:
        if path and os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------------------
# CLI parser tests
# ---------------------------------------------------------------------------

def test_cli_parser_accepts_extra_config_url(monkeypatch):
    # Make sure env var doesn't leak into this test
    monkeypatch.delenv("PR_AGENT_EXTRA_CONFIG_URL", raising=False)
    parser = set_parser()
    args = parser.parse_args([
        "--pr_url=https://example.com/pr/1",
        "--extra_config_url=https://config.example.com/shared.toml",
        "review",
    ])
    assert args.extra_config_url == "https://config.example.com/shared.toml"


def test_cli_parser_defaults_to_env_var_when_flag_omitted(monkeypatch):
    monkeypatch.setenv("PR_AGENT_EXTRA_CONFIG_URL", "/tmp/shared.toml")
    parser = set_parser()
    args = parser.parse_args(["--pr_url=https://example.com/pr/1", "review"])
    assert args.extra_config_url == "/tmp/shared.toml"


def test_cli_parser_omits_when_neither_flag_nor_env_set(monkeypatch):
    monkeypatch.delenv("PR_AGENT_EXTRA_CONFIG_URL", raising=False)
    parser = set_parser()
    args = parser.parse_args(["--pr_url=https://example.com/pr/1", "review"])
    assert args.extra_config_url is None


def test_cli_parser_flag_takes_precedence_over_env_var(monkeypatch):
    monkeypatch.setenv("PR_AGENT_EXTRA_CONFIG_URL", "/from/env.toml")
    parser = set_parser()
    args = parser.parse_args([
        "--pr_url=https://example.com/pr/1",
        "--extra_config_url=/from/flag.toml",
        "review",
    ])
    assert args.extra_config_url == "/from/flag.toml"


# ---------------------------------------------------------------------------
# Merge / precedence tests
#
# These exercise the actual settings merge done by _apply_settings_from_file
# and the precedence chain in apply_repo_settings(extra → repo-local).
# get_settings() is a process-wide singleton, so each test uses a fixture that
# snapshots and restores the sections it touches.
# ---------------------------------------------------------------------------

# Use bespoke section names so the tests can't be confused with any real
# configuration shipped by pr-agent.
_TEST_SECTION = "test_extra_config_section"
_TEST_KEYS_TO_RESTORE = [
    ("CONFIG", "EXTRA_CONFIG_URL"),
    (_TEST_SECTION.upper(), None),  # whole section
]


@pytest.fixture
def settings_sandbox():
    """Snapshot a few settings keys/sections, yield, restore on teardown."""
    settings = get_settings()
    saved = {}
    for section, key in _TEST_KEYS_TO_RESTORE:
        if key is None:
            saved[section] = settings.as_dict().get(section, None)
        else:
            saved[(section, key)] = settings.get(f"{section}.{key}", None)
    try:
        yield settings
    finally:
        # Restore sections/keys to pre-test state
        for section, key in _TEST_KEYS_TO_RESTORE:
            if key is None:
                settings.unset(section)
                if saved[section] is not None:
                    settings.set(section, saved[section], merge=False)
            else:
                val = saved[(section, key)]
                if val is None:
                    settings.unset(f"{section}.{key}")
                else:
                    settings.set(f"{section}.{key}", val)


def _write_toml(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content)
    return str(p)


# --- direct merge tests --------------------------------------------------

def test_apply_settings_file_adds_new_keys_to_settings(tmp_path, settings_sandbox):
    path = _write_toml(tmp_path, "extra.toml", f"""
[{_TEST_SECTION}]
alpha = "from-file"
beta = 42
""")
    _apply_settings_from_file(path, label="extra")

    assert get_settings().get(f"{_TEST_SECTION}.alpha") == "from-file"
    assert get_settings().get(f"{_TEST_SECTION}.beta") == 42


def test_apply_settings_file_overwrites_overlapping_keys(tmp_path, settings_sandbox):
    # Pre-seed an existing value to confirm the file overwrites it.
    get_settings().set(f"{_TEST_SECTION}.alpha", "original")
    get_settings().set(f"{_TEST_SECTION}.untouched", "keep-me")

    path = _write_toml(tmp_path, "extra.toml", f"""
[{_TEST_SECTION}]
alpha = "overwritten"
""")
    _apply_settings_from_file(path, label="extra")

    assert get_settings().get(f"{_TEST_SECTION}.alpha") == "overwritten"
    # Other keys in the same section are preserved by the section-level merge
    assert get_settings().get(f"{_TEST_SECTION}.untouched") == "keep-me"


def test_apply_settings_file_silently_skips_missing_path(settings_sandbox):
    # A canary value that must remain unchanged when the function is a no-op
    get_settings().set(f"{_TEST_SECTION}.canary", "untouched")
    _apply_settings_from_file("/no/such/file.toml", label="extra")
    assert get_settings().get(f"{_TEST_SECTION}.canary") == "untouched"


def test_apply_settings_file_does_not_log_secret_values(tmp_path, settings_sandbox):
    """
    Regression: the info log emitted after a merge must not include raw values
    from the merged config, otherwise secrets in external .pr_agent.toml
    (openai.key, gitlab.personal_access_token, etc.) leak into CI logs.

    pr-agent uses loguru; pytest's capsys/caplog don't capture it because the
    sink was bound to sys.stderr before pytest swapped it. Add a loguru sink
    directly so the test sees what would actually land in a real log.
    """
    from loguru import logger as loguru_logger

    secret_token = "glpat-supersecrettoken-shouldnotleak"
    openai_secret = "sk-also-secret-1234567890"
    path = _write_toml(tmp_path, "extra.toml", f"""
[gitlab]
personal_access_token = "{secret_token}"

[openai]
key = "{openai_secret}"
""")

    captured_lines = []
    sink_id = loguru_logger.add(
        lambda msg: captured_lines.append(str(msg)),
        level="DEBUG",
    )
    try:
        _apply_settings_from_file(path, label="extra")
    finally:
        loguru_logger.remove(sink_id)

    combined = "\n".join(captured_lines)

    assert secret_token not in combined, (
        "Secret value leaked into log output — _apply_settings_from_file must "
        "log section names only, never raw values."
    )
    assert openai_secret not in combined, "OpenAI key leaked into log output"

    # Section names *are* safe and useful for debugging — confirm they're emitted
    # (dynaconf upper-cases section keys, so accept either case).
    assert "gitlab" in combined.lower(), \
        "Expected the section name to appear in the merged-sections log line"


def test_apply_settings_file_silently_skips_invalid_toml(tmp_path, settings_sandbox):
    get_settings().set(f"{_TEST_SECTION}.canary", "untouched")
    path = _write_toml(tmp_path, "broken.toml", "this is = not valid toml = [[[")
    # custom_merge_loader logs the parse error with silent=True and produces an
    # empty merge — the existing canary value must survive.
    _apply_settings_from_file(path, label="extra")
    assert get_settings().get(f"{_TEST_SECTION}.canary") == "untouched"


# --- end-to-end precedence tests via apply_repo_settings ---------------------

class _FakeGitProvider:
    """Minimal stand-in for a git provider used by apply_repo_settings."""

    def __init__(self, repo_toml_bytes):
        self._repo_toml = repo_toml_bytes

    def get_repo_settings(self):
        return self._repo_toml


@pytest.fixture
def mock_git_provider(monkeypatch):
    """Replace get_git_provider_with_context with a factory the test controls."""
    holder = {"provider": _FakeGitProvider(b"")}

    def _factory(_pr_url):
        return holder["provider"]

    monkeypatch.setattr(git_utils, "get_git_provider_with_context", _factory)

    # Avoid the starlette_context cache between tests in this same process.
    try:
        from starlette_context import context as _ctx
        try:
            _ctx["repo_settings"] = None
        except Exception:
            pass
    except Exception:
        pass

    return holder


def test_precedence_repo_local_overrides_extra(tmp_path, settings_sandbox, mock_git_provider):
    """Keys defined in both files: repo-local wins."""
    extra_path = _write_toml(tmp_path, "extra.toml", f"""
[{_TEST_SECTION}]
shared_key = "from-extra"
extra_only = "extra-value"
""")
    repo_toml = f"""
[{_TEST_SECTION}]
shared_key = "from-repo"
repo_only = "repo-value"
""".encode()
    mock_git_provider["provider"] = _FakeGitProvider(repo_toml)

    get_settings().set("CONFIG.EXTRA_CONFIG_URL", extra_path)

    apply_repo_settings("https://example.com/pr/1")

    # repo wins on the shared key
    assert get_settings().get(f"{_TEST_SECTION}.shared_key") == "from-repo"
    # extra-only keys survive (extra was applied first, repo didn't touch this key)
    assert get_settings().get(f"{_TEST_SECTION}.extra_only") == "extra-value"
    # repo-only keys are present
    assert get_settings().get(f"{_TEST_SECTION}.repo_only") == "repo-value"


def test_extra_applied_when_repo_settings_empty(tmp_path, settings_sandbox, mock_git_provider):
    """If the repo has no .pr_agent.toml, extra values still take effect."""
    extra_path = _write_toml(tmp_path, "extra.toml", f"""
[{_TEST_SECTION}]
only_extra = "extra-wins"
""")
    mock_git_provider["provider"] = _FakeGitProvider(b"")  # empty / not found

    get_settings().set("CONFIG.EXTRA_CONFIG_URL", extra_path)

    apply_repo_settings("https://example.com/pr/1")

    assert get_settings().get(f"{_TEST_SECTION}.only_extra") == "extra-wins"


def test_repo_settings_apply_when_extra_url_unset(tmp_path, settings_sandbox, mock_git_provider):
    """Sanity: with no --extra_config_url, only repo-local config is applied."""
    repo_toml = f"""
[{_TEST_SECTION}]
repo_key = "repo-only"
""".encode()
    mock_git_provider["provider"] = _FakeGitProvider(repo_toml)

    # Explicitly ensure no extra URL is configured
    get_settings().set("CONFIG.EXTRA_CONFIG_URL", None)

    apply_repo_settings("https://example.com/pr/1")

    assert get_settings().get(f"{_TEST_SECTION}.repo_key") == "repo-only"


def test_unreachable_extra_url_does_not_block_repo_settings(
    tmp_path, settings_sandbox, mock_git_provider
):
    """If the extra source fails to resolve, repo-local config still applies."""
    repo_toml = f"""
[{_TEST_SECTION}]
repo_key = "still-applied"
""".encode()
    mock_git_provider["provider"] = _FakeGitProvider(repo_toml)

    get_settings().set("CONFIG.EXTRA_CONFIG_URL", "/nonexistent/path.toml")

    apply_repo_settings("https://example.com/pr/1")

    assert get_settings().get(f"{_TEST_SECTION}.repo_key") == "still-applied"
