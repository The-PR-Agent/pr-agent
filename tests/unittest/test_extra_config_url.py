import os
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


@pytest.fixture
def http_server():
    """Spin up _CapturingHandler on a free port for the duration of one test.

    Bind directly to port 0 and read the assigned port from the server. This
    avoids the race in "find a free port, close socket, bind HTTPServer to it"
    where another process can claim the port in the gap.
    """
    # Reset handler-level state so tests don't pollute each other.
    _CapturingHandler.body = SAMPLE_TOML
    _CapturingHandler.expected_path = "/shared.pr_agent.toml"
    _CapturingHandler.require_header = None
    _CapturingHandler.captured_headers = {}

    server = HTTPServer(("127.0.0.1", 0), _CapturingHandler)
    port = server.server_address[1]
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


def test_resolve_accepts_file_url_with_localhost_netloc(toml_on_disk):
    # file://localhost/<abs-path> is RFC 8089 form and must resolve same as file://
    path, is_temp = _resolve_extra_config_to_file(f"file://localhost{toml_on_disk}")
    assert path == toml_on_disk
    assert is_temp is False


def test_resolve_file_url_decodes_percent_encoded_path(tmp_path):
    # A real file at a path containing a space — file:// URL must percent-encode it.
    p = tmp_path / "name with space.toml"
    p.write_bytes(SAMPLE_TOML)
    url = f"file://{str(p).replace(' ', '%20')}"
    path, is_temp = _resolve_extra_config_to_file(url)
    assert path == str(p), "file:// percent-encoded path must be URL-decoded before stat()"
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
    fake_token = "TEST-AUTH-TOKEN-NOT-REAL"
    _CapturingHandler.require_header = ("Private-Token", fake_token)
    monkeypatch.setenv("PR_AGENT_EXTRA_CONFIG_AUTH_HEADER", f"PRIVATE-TOKEN: {fake_token}")

    url = f"{http_server}/shared.pr_agent.toml"
    path, is_temp = _resolve_extra_config_to_file(url)
    try:
        assert path is not None, "fetch should succeed when auth header is provided"
        assert is_temp is True
        # http.server normalizes header names to title-case
        assert _CapturingHandler.captured_headers.get("Private-Token") == fake_token
    finally:
        if path and os.path.exists(path):
            os.remove(path)


def test_resolve_returns_none_when_auth_header_missing(http_server, monkeypatch):
    _CapturingHandler.require_header = ("Private-Token", "TEST-AUTH-TOKEN-NOT-REAL")
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


def test_resolve_malformed_auth_header_warns_and_drops(http_server, monkeypatch):
    """Header without ':' is dropped, but the misconfiguration MUST be surfaced
    via a warning — silent fallthrough makes it impossible to diagnose why a
    private endpoint kept returning 401."""
    from loguru import logger as loguru_logger

    _CapturingHandler.require_header = None
    monkeypatch.setenv("PR_AGENT_EXTRA_CONFIG_AUTH_HEADER", "no-colon-here")

    captured_lines = []
    sink_id = loguru_logger.add(
        lambda msg: captured_lines.append(str(msg)),
        level="DEBUG",
    )

    url = f"{http_server}/shared.pr_agent.toml"
    try:
        path, is_temp = _resolve_extra_config_to_file(url)
    finally:
        loguru_logger.remove(sink_id)

    try:
        assert path is not None, "request should proceed even with malformed auth header"
        assert is_temp is True
        combined = "\n".join(captured_lines)
        assert "PR_AGENT_EXTRA_CONFIG_AUTH_HEADER" in combined and "malformed" in combined.lower(), (
            "Malformed auth header must produce a warning so misconfiguration is diagnosable"
        )
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


def test_cli_setting_reconciles_between_runs(settings_sandbox, monkeypatch):
    """Regression: in long-lived processes that call run() multiple times,
    a previously-set CONFIG.EXTRA_CONFIG_URL must not leak into the next call
    that omits the flag/env var. get_settings() is a process-wide singleton."""
    from argparse import Namespace

    import pr_agent.cli as cli_mod

    # Stub PRAgent so run() returns quickly without making network calls;
    # we only care about the synchronous setting-reconciliation prologue.
    class _StubAgent:
        async def handle_request(self, *_args, **_kwargs):
            return True

    monkeypatch.setattr(cli_mod, "PRAgent", lambda: _StubAgent())
    monkeypatch.delenv("PR_AGENT_EXTRA_CONFIG_URL", raising=False)

    # First invocation: explicit URL — should populate the singleton key
    cli_mod.run(args=Namespace(
        pr_url="https://example.com/pr/1",
        issue_url=None,
        extra_config_url="/first/run.toml",
        command="review",
        rest=[],
    ))
    assert get_settings().get("CONFIG.EXTRA_CONFIG_URL") == "/first/run.toml"

    # Second invocation: no URL — singleton key must be CLEARED, not carried over
    cli_mod.run(args=Namespace(
        pr_url="https://example.com/pr/1",
        issue_url=None,
        extra_config_url=None,
        command="review",
        rest=[],
    ))
    assert get_settings().get("CONFIG.EXTRA_CONFIG_URL") in (None, ""), (
        "CONFIG.EXTRA_CONFIG_URL must be cleared when the flag/env var is "
        f"absent; got {get_settings().get('CONFIG.EXTRA_CONFIG_URL')!r}"
    )


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


_AUTO_CAST_ENV = "AUTO_CAST_FOR_DYNACONF"


@pytest.fixture
def settings_sandbox():
    """Snapshot a few settings keys/sections AND the AUTO_CAST_FOR_DYNACONF env
    var (which apply_repo_settings() mutates), yield, restore on teardown.

    Both are process-global state — settings via the Dynaconf singleton and
    env vars via os.environ — so any test that calls apply_repo_settings() can
    otherwise leak state into sibling tests."""
    settings = get_settings()
    saved = {}
    for section, key in _TEST_KEYS_TO_RESTORE:
        if key is None:
            saved[section] = settings.as_dict().get(section, None)
        else:
            saved[(section, key)] = settings.get(f"{section}.{key}", None)

    # Snapshot the env var; use a sentinel to distinguish "unset" from "''"
    _UNSET = object()
    saved_env = os.environ.get(_AUTO_CAST_ENV, _UNSET)

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

        # Restore the env var
        if saved_env is _UNSET:
            os.environ.pop(_AUTO_CAST_ENV, None)
        else:
            os.environ[_AUTO_CAST_ENV] = saved_env


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

    Uses bespoke sandboxed section names instead of real [gitlab]/[openai]
    sections so the test cannot pollute settings for sibling tests sharing the
    process-wide Dynaconf singleton.

    pr-agent uses loguru; pytest's capsys/caplog don't capture it because the
    sink was bound to sys.stderr before pytest swapped it. Add a loguru sink
    directly so the test sees what would actually land in a real log.
    """
    from loguru import logger as loguru_logger

    # Sentinels that don't match real token prefixes (glpat-, sk-, ...) so
    # secret scanners don't flag the test file itself.
    secret_token = "SENTINEL-EXTRA-CONFIG-PAT-SHOULD-NOT-LEAK"
    openai_secret = "SENTINEL-EXTRA-CONFIG-OPENAI-KEY-SHOULD-NOT-LEAK"
    path = _write_toml(tmp_path, "extra.toml", f"""
[{_TEST_SECTION}]
fake_personal_access_token = "{secret_token}"
fake_api_key = "{openai_secret}"
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
    assert openai_secret not in combined, "API-key-shaped value leaked into log output"

    # Section name *is* safe and useful for debugging — confirm it's emitted
    # (dynaconf upper-cases section keys, so compare case-insensitively).
    assert _TEST_SECTION.lower() in combined.lower(), \
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


def test_env_var_overrides_extra_config(tmp_path, settings_sandbox, monkeypatch):
    """Regression: env vars are the highest-precedence layer (per the docs'
    precedence chain). The section-level overwrite inside
    _apply_settings_from_file() must not silently clobber values originally
    sourced from environment variables — otherwise env-supplied secrets
    (provider tokens, API keys) get replaced by extra-config values."""
    env_key = f"{_TEST_SECTION.upper()}__TOKEN"
    monkeypatch.setenv(env_key, "from-env")

    # Re-apply env so the sandbox's pristine state reflects the env value
    # (Dynaconf only auto-loads at construction time).
    from dynaconf.loaders import env_loader as _env_loader
    _env_loader.load(get_settings())
    assert get_settings().get(f"{_TEST_SECTION}.token") == "from-env", (
        "precondition: env var must populate the section before the merge"
    )

    path = _write_toml(tmp_path, "extra.toml", f"""
[{_TEST_SECTION}]
token = "from-extra-file"
other = "not-in-env"
""")
    _apply_settings_from_file(path, label="extra")

    assert get_settings().get(f"{_TEST_SECTION}.token") == "from-env", (
        "env-sourced value must survive an extra-config merge — otherwise a "
        "provider token from PR_AGENT_<SECTION>__<KEY> can be silently "
        "replaced by the external .toml"
    )
    # Non-overlapping keys from the file still take effect
    assert get_settings().get(f"{_TEST_SECTION}.other") == "not-in-env"


def test_env_var_overrides_repo_settings(tmp_path, settings_sandbox, mock_git_provider, monkeypatch):
    """Same precedence rule applies to the repo-local .pr_agent.toml merge:
    env vars must remain highest priority even after repo settings are merged
    on top of the extra config."""
    env_key = f"{_TEST_SECTION.upper()}__TOKEN"
    monkeypatch.setenv(env_key, "from-env")
    from dynaconf.loaders import env_loader as _env_loader
    _env_loader.load(get_settings())

    repo_toml = f"""
[{_TEST_SECTION}]
token = "from-repo-file"
repo_only = "repo-value"
""".encode()
    mock_git_provider["provider"] = _FakeGitProvider(repo_toml)

    apply_repo_settings("https://example.com/pr/1")

    assert get_settings().get(f"{_TEST_SECTION}.token") == "from-env", (
        "env-sourced value must survive a repo-local merge as well"
    )
    assert get_settings().get(f"{_TEST_SECTION}.repo_only") == "repo-value"


def test_env_var_visible_to_git_provider_after_extra_merge(
    tmp_path, settings_sandbox, monkeypatch
):
    """Provider __init__ may read auth settings (e.g. GITLAB.PERSONAL_ACCESS_TOKEN).
    Env-sourced credentials must therefore be restored BEFORE the git provider
    is constructed, even if the extra config tried to overwrite them."""
    import pr_agent.git_providers.utils as utils_mod

    env_key = f"{_TEST_SECTION.upper()}__TOKEN"
    monkeypatch.setenv(env_key, "env-secret")
    from dynaconf.loaders import env_loader as _env_loader
    _env_loader.load(get_settings())

    extra_path = _write_toml(tmp_path, "extra.toml", f"""
[{_TEST_SECTION}]
token = "extra-config-value"
""")
    get_settings().set("CONFIG.EXTRA_CONFIG_URL", extra_path)

    seen = {}

    class _Provider:
        def __init__(self):
            seen["token_at_init"] = get_settings().get(f"{_TEST_SECTION}.token")

        def get_repo_settings(self):
            return b""

    monkeypatch.setattr(utils_mod, "get_git_provider_with_context", lambda _: _Provider())

    apply_repo_settings("https://example.com/pr/1")

    assert seen["token_at_init"] == "env-secret", (
        "provider __init__ must see the env-sourced credential, not the value "
        f"the extra config tried to inject; saw {seen['token_at_init']!r}"
    )


# ---------------------------------------------------------------------------
# Regression tests for review feedback (review round 2)
# ---------------------------------------------------------------------------

def test_resolve_accepts_windows_drive_letter_path(monkeypatch):
    """A bare Windows path like 'C:\\\\shared.toml' must be treated as a local
    path, not as a URL with scheme 'c:'. urlparse() would otherwise route it
    into the unsupported-scheme branch."""
    fake_win_path = r"C:\Users\dev\shared.toml"

    # We can't create a real C:\ file on macOS/Linux CI, so stub the existence
    # check just for that path.
    real_isfile = os.path.isfile
    monkeypatch.setattr(
        "pr_agent.git_providers.utils.os.path.isfile",
        lambda p: True if p == fake_win_path else real_isfile(p),
    )

    path, is_temp = _resolve_extra_config_to_file(fake_win_path)
    assert path == fake_win_path, "Windows drive-letter path must be returned unchanged"
    assert is_temp is False

    # Forward-slash variant (D:/path/x.toml) must also be recognised
    fake_fwd = "D:/cfg/shared.toml"
    monkeypatch.setattr(
        "pr_agent.git_providers.utils.os.path.isfile",
        lambda p: True if p in (fake_win_path, fake_fwd) else real_isfile(p),
    )
    path, is_temp = _resolve_extra_config_to_file(fake_fwd)
    assert path == fake_fwd
    assert is_temp is False


def test_resolve_warns_when_windows_path_missing(monkeypatch):
    """If a Windows-style path doesn't exist, we warn (same as any local path)
    instead of falling through to the unsupported-scheme branch."""
    from loguru import logger as loguru_logger

    captured = []
    sink_id = loguru_logger.add(lambda m: captured.append(str(m)), level="DEBUG")
    try:
        path, is_temp = _resolve_extra_config_to_file(r"C:\does\not\exist.toml")
    finally:
        loguru_logger.remove(sink_id)

    assert path is None
    combined = "\n".join(captured)
    assert "not found at local path" in combined, (
        "Windows path miss must produce the local-path-not-found warning, not "
        "the unsupported-scheme warning"
    )
    assert "Unsupported scheme" not in combined


def test_resolve_rejects_non_string_source():
    """CONFIG.EXTRA_CONFIG_URL set to a non-string must be rejected at the
    boundary, not bubble up an exception from urlparse()."""
    for bad in [42, ["https://x.example/y"], {"url": "x"}, True]:
        path, is_temp = _resolve_extra_config_to_file(bad)
        assert path is None and is_temp is False, (
            f"non-string source {bad!r} must be rejected"
        )


def test_safe_url_for_log_strips_credentials():
    """URLs containing userinfo or token-bearing query params must be
    sanitised before going to the log."""
    from pr_agent.git_providers.utils import _safe_url_for_log

    raw = "https://alice:s3cret@config.example.com:8443/pr-agent/shared.toml?private_token=xyz"
    safe = _safe_url_for_log(raw)
    # Userinfo and query string must be stripped
    assert "alice" not in safe
    assert "s3cret" not in safe
    assert "private_token" not in safe
    assert "xyz" not in safe
    # Scheme, host, port and path remain (useful for debugging)
    assert safe == "https://config.example.com:8443/pr-agent/shared.toml"


def test_resolve_does_not_log_url_credentials(http_server, monkeypatch):
    """Regression: even when the URL embeds userinfo, the log lines emitted by
    _resolve_extra_config_to_file must not contain those credentials."""
    from loguru import logger as loguru_logger

    secret_in_url = "userinfo-secret-xyz"
    # http.server doesn't honor userinfo for auth, so we wire the path so the
    # request 404s and triggers the failure-path log line — that's the one we
    # want to assert never contains the secret.
    base = http_server  # http://127.0.0.1:<port>
    netloc = base.split("//", 1)[1]
    url = f"http://baduser:{secret_in_url}@{netloc}/does-not-exist.toml"

    captured = []
    sink_id = loguru_logger.add(lambda msg: captured.append(str(msg)), level="DEBUG")
    try:
        path, is_temp = _resolve_extra_config_to_file(url)
    finally:
        loguru_logger.remove(sink_id)

    assert path is None
    combined = "\n".join(captured)
    assert secret_in_url not in combined, "URL userinfo leaked into log output"
    assert "baduser" not in combined, "URL username leaked into log output"


def test_apply_settings_file_loads_when_dynaconf_lacks_security_flags(
    tmp_path, settings_sandbox, monkeypatch
):
    """Regression: older Dynaconf versions reject load_dotenv/envvar_prefix
    kwargs. _apply_settings_from_file must still load the file via the fallback
    instead of dropping the merge entirely."""
    import pr_agent.git_providers.utils as utils_mod

    real_dynaconf = utils_mod.Dynaconf
    call_log = {"strict": 0, "fallback": 0}

    class _FakeDynaconf:
        def __init__(self, *args, **kwargs):
            if "load_dotenv" in kwargs or "envvar_prefix" in kwargs:
                call_log["strict"] += 1
                raise TypeError("simulated older Dynaconf — load_dotenv unsupported")
            call_log["fallback"] += 1
            self._real = real_dynaconf(*args, **kwargs)

        def as_dict(self):
            return self._real.as_dict()

    monkeypatch.setattr(utils_mod, "Dynaconf", _FakeDynaconf)

    path = _write_toml(tmp_path, "extra.toml", f"""
[{_TEST_SECTION}]
fallback_key = "from-old-dynaconf"
""")
    _apply_settings_from_file(path, label="extra")

    assert call_log["strict"] == 1, "must first try the hardened Dynaconf kwargs"
    assert call_log["fallback"] == 1, "must fall back when TypeError is raised"
    assert get_settings().get(f"{_TEST_SECTION}.fallback_key") == "from-old-dynaconf"


def test_settings_sandbox_restores_auto_cast_env_var(monkeypatch):
    """Regression: apply_repo_settings() sets os.environ['AUTO_CAST_FOR_DYNACONF']
    as a side effect. The settings_sandbox fixture must restore that env var
    to its prior state so it can't leak into sibling tests.

    We exercise the fixture indirectly: build a fresh instance inline, call
    apply_repo_settings inside it (with a stubbed provider), confirm the env
    var is set during the test, and confirm it's restored after teardown.
    """
    import pr_agent.git_providers.utils as utils_mod

    # Save what the outer process state actually is right now
    _UNSET = object()
    pre_state = os.environ.get(_AUTO_CAST_ENV, _UNSET)

    class _FakeGP:
        def get_repo_settings(self):
            return b""

    monkeypatch.setattr(utils_mod, "get_git_provider_with_context", lambda _: _FakeGP())

    # Force a known pre-state we can check restoration against
    if pre_state is _UNSET:
        os.environ.pop(_AUTO_CAST_ENV, None)
        expected_after = _UNSET
    else:
        expected_after = pre_state

    # Manually drive the fixture's generator lifecycle so we can assert state
    # both during and after.
    gen = settings_sandbox.__wrapped__()
    next(gen)  # equivalent to entering the with-block
    try:
        apply_repo_settings("https://example.com/pr/1")
        assert os.environ.get(_AUTO_CAST_ENV) == "false", (
            "apply_repo_settings must set AUTO_CAST_FOR_DYNACONF during the test"
        )
    finally:
        try:
            next(gen)
        except StopIteration:
            pass

    after = os.environ.get(_AUTO_CAST_ENV, _UNSET)
    assert after == expected_after, (
        "settings_sandbox must restore AUTO_CAST_FOR_DYNACONF to its prior state "
        f"(expected {expected_after!r}, got {after!r})"
    )


def test_apply_settings_file_security_check_runs_on_fallback_path(
    tmp_path, settings_sandbox, monkeypatch
):
    """Regression: when the TypeError fallback fires, the bypass Dynaconf call
    does not use custom_merge_loader, so validate_file_security() would not run
    via the normal loader. The function must pre-validate the file explicitly
    so forbidden directives (includes/preloads/loaders) still cannot slip
    through on affected Dynaconf versions."""
    import pr_agent.git_providers.utils as utils_mod

    real_dynaconf = utils_mod.Dynaconf
    call_log = {"strict": 0, "fallback": 0}

    class _FakeDynaconf:
        def __init__(self, *args, **kwargs):
            if "load_dotenv" in kwargs or "envvar_prefix" in kwargs:
                call_log["strict"] += 1
                raise TypeError("simulated older Dynaconf")
            call_log["fallback"] += 1
            self._real = real_dynaconf(*args, **kwargs)

        def as_dict(self):
            return self._real.as_dict()

    monkeypatch.setattr(utils_mod, "Dynaconf", _FakeDynaconf)

    # A file containing a forbidden directive that custom_merge_loader's
    # validate_file_security() must reject.
    malicious = _write_toml(tmp_path, "evil.toml", f"""
[{_TEST_SECTION}]
includes = ["/etc/passwd"]
benign_key = "should-not-be-applied"
""")

    sentinel = "PRE-FALLBACK-VALUE"
    get_settings().set(f"{_TEST_SECTION}.benign_key", sentinel)
    _apply_settings_from_file(malicious, label="extra")

    assert call_log["strict"] == 1, "hardened Dynaconf path must be attempted first"
    assert call_log["fallback"] == 0, (
        "fallback must NOT run after security pre-validation rejects the file"
    )
    # The malicious file's keys must not have been applied
    assert get_settings().get(f"{_TEST_SECTION}.benign_key") == sentinel, (
        "values from a file with forbidden directives must not be merged"
    )


def test_env_var_overrides_extra_config_when_default_exists(
    tmp_path, settings_sandbox, monkeypatch
):
    """Regression: when a key has a baseline value (from configuration.toml),
    the env_loader overwrites it case-insensitively at the EXISTING lowercase
    key. _apply_settings_from_file() then sees that same lowercase key in the
    file and was silently overwriting the env value — because as_dict().get()
    on the section is case-sensitive (returns {} when section name comes from
    a lowercase TOML header) and unset()+set() did the rest.

    This case is what makes the bug observable in production: real secrets
    (e.g. gitlab.url, openai.key) all have defaults in configuration.toml, so
    they hit this code path."""
    settings = get_settings()
    section_dict_key = _TEST_SECTION.upper()

    # Simulate a configuration.toml-like default at the lowercase key.
    settings.set(_TEST_SECTION, {"endpoint": "default-endpoint"}, merge=False)
    # Track the section for cleanup via the existing sandbox fixture.

    # Env loader overwrites the lowercase 'endpoint' key in place.
    env_key = f"{section_dict_key}__ENDPOINT"
    monkeypatch.setenv(env_key, "from-env")
    from dynaconf.loaders import env_loader as _env_loader
    _env_loader.load(settings)
    assert settings.get(f"{_TEST_SECTION}.endpoint") == "from-env", (
        "precondition: env var must overwrite the default value"
    )

    path = _write_toml(tmp_path, "extra.toml", f"""
[{_TEST_SECTION}]
endpoint = "from-extra-file"
""")
    _apply_settings_from_file(path, label="extra")

    assert settings.get(f"{_TEST_SECTION}.endpoint") == "from-env", (
        "env-sourced value must survive an extra-config merge even when the "
        "key has a baseline default; otherwise file values silently replace "
        "env-supplied secrets (e.g. gitlab.url, openai.key)"
    )


def test_env_var_overrides_repo_settings_when_default_exists(
    tmp_path, settings_sandbox, mock_git_provider, monkeypatch
):
    """Same fragility, but exercised through the full apply_repo_settings()
    path: env vars must still win over repo-local .pr_agent.toml for keys
    that have a baseline default."""
    settings = get_settings()
    settings.set(_TEST_SECTION, {"endpoint": "default-endpoint"}, merge=False)

    env_key = f"{_TEST_SECTION.upper()}__ENDPOINT"
    monkeypatch.setenv(env_key, "from-env")
    from dynaconf.loaders import env_loader as _env_loader
    _env_loader.load(settings)

    repo_toml = f"""
[{_TEST_SECTION}]
endpoint = "from-repo-file"
""".encode()
    mock_git_provider["provider"] = _FakeGitProvider(repo_toml)

    apply_repo_settings("https://example.com/pr/1")

    assert settings.get(f"{_TEST_SECTION}.endpoint") == "from-env", (
        "env-sourced value must survive a repo-local merge for keys with "
        "baseline defaults too"
    )


def test_extra_config_applied_before_git_provider(tmp_path, settings_sandbox, monkeypatch):
    """Regression: provider __init__ may read settings (e.g. GITLAB token), so
    the extra config must be merged BEFORE get_git_provider_with_context() is
    called. We assert ordering by recording the order of side effects."""
    import pr_agent.git_providers.utils as utils_mod

    events = []

    extra_path = _write_toml(tmp_path, "extra.toml", f"""
[{_TEST_SECTION}]
provider_critical = "from-extra"
""")
    get_settings().set("CONFIG.EXTRA_CONFIG_URL", extra_path)

    # The fake provider records the setting value visible at construction time.
    class _OrderRecordingProvider:
        def __init__(self):
            events.append(
                (
                    "provider_init_sees",
                    get_settings().get(f"{_TEST_SECTION}.provider_critical"),
                )
            )

        def get_repo_settings(self):
            return b""

    def _factory(_pr_url):
        events.append(("get_git_provider_with_context",))
        return _OrderRecordingProvider()

    monkeypatch.setattr(utils_mod, "get_git_provider_with_context", _factory)

    apply_repo_settings("https://example.com/pr/1")

    # The provider must be constructed AFTER the extra config is applied, so
    # the merged value is visible inside provider __init__.
    init_event = next(e for e in events if e[0] == "provider_init_sees")
    assert init_event[1] == "from-extra", (
        "extra_config_url must be merged before the git provider is constructed; "
        f"provider saw {init_event[1]!r}"
    )
