"""Tests for dashboard run-control routes — never spawn a real process."""
from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from pr_dashboard import app as app_module
from pr_dashboard import recorder, redaction, registry, runner, store
from pr_dashboard.registry import Repo

_ORIGIN = "http://127.0.0.1"
_SAFE_HEADERS = {"Origin": _ORIGIN}
_TOKEN_RE = re.compile(r"/runs/([0-9a-f-]{36})")


@pytest.fixture(autouse=True)
def _reset_processes():
    """_PROCESSES is a module-level dict; keep it from bleeding across tests."""
    runner._PROCESSES.clear()
    yield
    runner._PROCESSES.clear()


def _client(tmp_path, monkeypatch, *, record_runs=False):
    registry.save([Repo(provider="github", slug="owner/repo")], tmp_path / "pr_dashboard.toml")
    monkeypatch.setattr(recorder, "recording_enabled", lambda: record_runs)
    monkeypatch.setattr(runner, "_default_log_dir", lambda: tmp_path / "logs")
    application = app_module.create_app(
        registry_path=tmp_path / "pr_dashboard.toml", db_path=tmp_path / "usage.db"
    )
    return TestClient(application, base_url=_ORIGIN)


def _csrf_token(client, path="/runs"):
    response = client.get(path)
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match, f"csrf_token input missing from {path}"
    return match.group(1)


def _post_runs(client, data):
    token = _csrf_token(client)
    payload = {**data, "csrf_token": token}
    return client.post("/runs", data=payload, headers=_SAFE_HEADERS)


class _FakeProc:
    def __init__(self, poll_result):
        self.pid = 4242
        self._poll_result = poll_result
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._poll_result

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return self._poll_result if self._poll_result is not None else 0

    def kill(self):
        self.killed = True


def _fake_popen(monkeypatch, *, poll_result=None):
    """Install a Popen spy; returns the list of recorded calls."""
    calls = []

    def spy(argv, **kwargs):
        proc = _FakeProc(poll_result)
        calls.append({"argv": argv, "kwargs": kwargs, "proc": proc})
        return proc

    monkeypatch.setattr(runner.subprocess, "Popen", spy)
    return calls


class TestStartRun:
    def test_valid_post_writes_running_row_and_renders_run_page(self, tmp_path, monkeypatch):
        """A valid POST /runs launches once and lands on that token's run page"""
        client = _client(tmp_path, monkeypatch)
        calls = _fake_popen(monkeypatch, poll_result=None)
        response = _post_runs(
            client, {"provider": "github", "slug": "owner/repo", "number": "7", "command": "review"}
        )
        assert response.status_code == 200
        assert len(calls) == 1
        conn = store.connect(tmp_path / "usage.db")
        rows = store.list_ui_runs(conn)
        assert len(rows) == 1
        assert rows[0]["status"] == "running"
        assert rows[0]["token"] in response.text

    def test_bad_command_reports_error_and_never_spawns(self, tmp_path, monkeypatch):
        """An unsupported command is rejected with a message and no process is launched"""
        client = _client(tmp_path, monkeypatch)
        calls = _fake_popen(monkeypatch)
        response = _post_runs(
            client, {"provider": "github", "slug": "owner/repo", "number": "1", "command": "rm"}
        )
        assert response.status_code == 200
        assert "unsupported command" in response.text
        assert calls == []


class TestRunDetailPolling:
    def test_running_run_has_polling_attr(self, tmp_path, monkeypatch):
        """A non-terminal run's detail page carries hx-trigger"""
        client = _client(tmp_path, monkeypatch)
        _fake_popen(monkeypatch, poll_result=None)
        response = _post_runs(
            client, {"provider": "github", "slug": "owner/repo", "number": "1", "command": "review"}
        )
        assert "hx-trigger" in response.text

    def test_terminal_run_has_no_polling_attr(self, tmp_path, monkeypatch):
        """A terminal run's detail HTML contains no hx-trigger"""
        client = _client(tmp_path, monkeypatch)
        _fake_popen(monkeypatch, poll_result=None)
        response = _post_runs(
            client, {"provider": "github", "slug": "owner/repo", "number": "1", "command": "review"}
        )
        token = _TOKEN_RE.search(response.text).group(1)
        conn = store.connect(tmp_path / "usage.db")
        store.finish_ui_run(conn, token=token, status="ok", exit_code=0, finished_at="2026-09-11T12:00:00Z")
        detail = client.get(f"/runs/{token}")
        assert "hx-trigger" not in detail.text


class TestLogRedaction:
    def test_log_tail_is_redacted(self, tmp_path, monkeypatch):
        """A secret in the log tail is replaced by *** and never shown raw"""
        client = _client(tmp_path, monkeypatch)
        _fake_popen(monkeypatch, poll_result=None)
        secret = "ghp_SuperSecretFakeTokenForTest1234567890"
        monkeypatch.setattr(redaction, "secret_values", lambda: [secret])
        response = _post_runs(
            client, {"provider": "github", "slug": "owner/repo", "number": "1", "command": "review"}
        )
        token = _TOKEN_RE.search(response.text).group(1)
        log_path = tmp_path / "logs" / f"{token}.log"
        log_path.write_text(f"line one\ntoken leaked: {secret}\n")
        detail = client.get(f"/runs/{token}")
        assert secret not in detail.text
        assert "***" in detail.text


class TestAccountingStates:
    def test_accounting_row_present_renders_tokens_and_cost(self, tmp_path, monkeypatch):
        """An accounting row joined by token renders tokens/cost, not the other two messages"""
        client = _client(tmp_path, monkeypatch, record_runs=True)
        _fake_popen(monkeypatch, poll_result=0)
        response = _post_runs(
            client, {"provider": "github", "slug": "owner/repo", "number": "1", "command": "review"}
        )
        token = _TOKEN_RE.search(response.text).group(1)
        conn = store.connect(tmp_path / "usage.db")
        store.start_run(
            conn, provider="github", command="review", pr_url="https://github.com/owner/repo/pull/1",
            repo_slug="owner/repo", pr_number=1, started_at="2026-09-11T12:00:00Z",
            dashboard_token=token,
        )
        detail = client.get(f"/runs/{token}")
        assert "Tokens:" in detail.text
        assert "Accounting is off for this run" not in detail.text
        assert "The run ended before accounting started" not in detail.text

    def test_accounting_disabled_shows_not_enabled_message(self, tmp_path, monkeypatch):
        """No accounting row, and recording is off: 'Accounting is off for this run'"""
        client = _client(tmp_path, monkeypatch, record_runs=False)
        _fake_popen(monkeypatch, poll_result=0)
        response = _post_runs(
            client, {"provider": "github", "slug": "owner/repo", "number": "1", "command": "review"}
        )
        token = _TOKEN_RE.search(response.text).group(1)
        detail = client.get(f"/runs/{token}")
        assert "Accounting is off for this run" in detail.text
        assert "Tokens:" not in detail.text
        assert "The run ended before accounting started" not in detail.text

    def test_accounting_enabled_but_no_row_shows_ended_before_message(self, tmp_path, monkeypatch):
        """No accounting row, but recording is on: 'The run ended before accounting started'"""
        client = _client(tmp_path, monkeypatch, record_runs=True)
        _fake_popen(monkeypatch, poll_result=0)
        response = _post_runs(
            client, {"provider": "github", "slug": "owner/repo", "number": "1", "command": "review"}
        )
        token = _TOKEN_RE.search(response.text).group(1)
        detail = client.get(f"/runs/{token}")
        assert "The run ended before accounting started" in detail.text
        assert "Tokens:" not in detail.text
        assert "Accounting is off for this run" not in detail.text


class TestCancelRun:
    def test_cancel_records_status_cancelled(self, tmp_path, monkeypatch):
        """POST /runs/{token}/cancel records status cancelled"""
        client = _client(tmp_path, monkeypatch)
        _fake_popen(monkeypatch, poll_result=None)
        response = _post_runs(
            client, {"provider": "github", "slug": "owner/repo", "number": "1", "command": "review"}
        )
        token = _TOKEN_RE.search(response.text).group(1)
        csrf = _csrf_token(client, path=f"/runs/{token}")
        cancel_response = client.post(
            f"/runs/{token}/cancel", data={"csrf_token": csrf}, headers=_SAFE_HEADERS
        )
        assert cancel_response.status_code == 200
        conn = store.connect(tmp_path / "usage.db")
        assert store.get_ui_run(conn, token)["status"] == "cancelled"


class TestCsrfGuards:
    def test_post_runs_with_no_origin_is_refused_and_never_spawns(self, tmp_path, monkeypatch):
        """POST /runs with no Origin header is refused, and Popen is never called"""
        client = _client(tmp_path, monkeypatch)
        calls = _fake_popen(monkeypatch)
        csrf = _csrf_token(client)
        response = client.post(
            "/runs",
            data={"provider": "github", "slug": "owner/repo", "number": "1", "command": "review",
                  "csrf_token": csrf},
        )
        assert response.status_code == 403
        assert calls == []

    def test_post_runs_with_foreign_origin_is_refused_and_never_spawns(self, tmp_path, monkeypatch):
        """POST /runs with a foreign Origin is refused, and Popen is never called"""
        client = _client(tmp_path, monkeypatch)
        calls = _fake_popen(monkeypatch)
        csrf = _csrf_token(client)
        response = client.post(
            "/runs",
            data={"provider": "github", "slug": "owner/repo", "number": "1", "command": "review",
                  "csrf_token": csrf},
            headers={"Origin": "http://evil.example"},
        )
        assert response.status_code == 403
        assert calls == []

    def test_post_cancel_with_no_origin_is_refused_and_never_spawns(self, tmp_path, monkeypatch):
        """POST /runs/{token}/cancel with no Origin header is refused, and no new spawn occurs"""
        client = _client(tmp_path, monkeypatch)
        calls = _fake_popen(monkeypatch, poll_result=None)
        response = _post_runs(
            client, {"provider": "github", "slug": "owner/repo", "number": "1", "command": "review"}
        )
        token = _TOKEN_RE.search(response.text).group(1)
        calls_before = len(calls)
        csrf = _csrf_token(client, path=f"/runs/{token}")
        cancel_response = client.post(f"/runs/{token}/cancel", data={"csrf_token": csrf})
        assert cancel_response.status_code == 403
        assert len(calls) == calls_before

    def test_post_cancel_with_foreign_origin_is_refused_and_never_spawns(self, tmp_path, monkeypatch):
        """POST /runs/{token}/cancel with a foreign Origin is refused, and no new spawn occurs"""
        client = _client(tmp_path, monkeypatch)
        calls = _fake_popen(monkeypatch, poll_result=None)
        response = _post_runs(
            client, {"provider": "github", "slug": "owner/repo", "number": "1", "command": "review"}
        )
        token = _TOKEN_RE.search(response.text).group(1)
        calls_before = len(calls)
        csrf = _csrf_token(client, path=f"/runs/{token}")
        cancel_response = client.post(
            f"/runs/{token}/cancel", data={"csrf_token": csrf}, headers={"Origin": "http://evil.example"}
        )
        assert cancel_response.status_code == 403
        assert len(calls) == calls_before


class TestReap:
    def test_reap_moves_finished_children_and_orphans(self, tmp_path):
        """A finished child moves to ok/failed by its exit code; an orphan row becomes failed"""
        conn = store.connect(tmp_path / "usage.db")
        for token, number in (("ok-tok", 1), ("fail-tok", 2), ("orphan-tok", 3)):
            store.start_ui_run(
                conn, token=token, provider="github", repo_slug="o/r", pr_number=number,
                pr_url=f"https://github.com/o/r/pull/{number}", command="review",
                log_path=str(tmp_path / f"{token}.log"), started_at="2026-09-11T12:00:00Z",
            )
            conn.execute("UPDATE ui_runs SET status = 'running' WHERE token = ?", (token,))

        runner._PROCESSES["ok-tok"] = MagicMock(poll=MagicMock(return_value=0))
        runner._PROCESSES["fail-tok"] = MagicMock(poll=MagicMock(return_value=1))
        # orphan-tok is deliberately absent from _PROCESSES.

        runner.reap(conn)

        ok_row = store.get_ui_run(conn, "ok-tok")
        assert ok_row["status"] == "ok"
        assert ok_row["exit_code"] == 0
        fail_row = store.get_ui_run(conn, "fail-tok")
        assert fail_row["status"] == "failed"
        assert fail_row["exit_code"] == 1
        orphan_row = store.get_ui_run(conn, "orphan-tok")
        assert orphan_row["status"] == "failed"
        assert orphan_row["exit_code"] is None


class TestRedactionUnavailable:
    def test_the_page_withholds_the_log_instead_of_returning_500(self, tmp_path, monkeypatch):
        """An unreadable secret inventory yields a message and no log bytes, not a 500"""
        client = _client(tmp_path, monkeypatch)
        _fake_popen(monkeypatch, poll_result=None)
        response = _post_runs(
            client, {"provider": "github", "slug": "owner/repo", "number": "1", "command": "review"}
        )
        token = _TOKEN_RE.search(response.text).group(1)
        (tmp_path / "logs" / f"{token}.log").write_text("line one\nghp_leaked_value_here\n")

        def unavailable():
            raise redaction.RedactionUnavailable("the secret inventory could not be read")

        monkeypatch.setattr(redaction, "secret_values", unavailable)
        detail = client.get(f"/runs/{token}")

        assert detail.status_code == 200
        # The absence assertion is the point: a page that rendered the raw log would also
        # contain the message, so asserting the message alone proves nothing.
        assert "ghp_leaked_value_here" not in detail.text
        assert "line one" not in detail.text
        assert "the secret inventory could not be read" in detail.text


class TestTimeoutMessage:
    def test_a_timed_out_run_says_so_on_the_page(self, tmp_path, monkeypatch):
        """A failed run whose wall time reached the timeout is labelled as timed out"""
        client = _client(tmp_path, monkeypatch)
        conn = store.connect(tmp_path / "usage.db")
        store.start_ui_run(
            conn, token="slow-tok", provider="github", repo_slug="owner/repo", pr_number=1,
            pr_url="https://github.com/owner/repo/pull/1", command="review",
            log_path=str(tmp_path / "slow.log"), started_at="2026-09-11T12:00:00+00:00",
        )
        store.finish_ui_run(
            conn, token="slow-tok", status="failed", exit_code=-9,
            finished_at="2026-09-11T12:30:00+00:00",
        )
        detail = client.get("/runs/slow-tok")
        assert "timed out" in detail.text

    def test_a_quickly_failed_run_is_not_labelled_timed_out(self, tmp_path, monkeypatch):
        """A run that failed in seconds must not claim it hit the timeout"""
        client = _client(tmp_path, monkeypatch)
        conn = store.connect(tmp_path / "usage.db")
        store.start_ui_run(
            conn, token="fast-tok", provider="github", repo_slug="owner/repo", pr_number=2,
            pr_url="https://github.com/owner/repo/pull/2", command="review",
            log_path=str(tmp_path / "fast.log"), started_at="2026-09-11T12:00:00+00:00",
        )
        store.finish_ui_run(
            conn, token="fast-tok", status="failed", exit_code=1,
            finished_at="2026-09-11T12:00:04+00:00",
        )
        detail = client.get("/runs/fast-tok")
        assert "timed out" not in detail.text
