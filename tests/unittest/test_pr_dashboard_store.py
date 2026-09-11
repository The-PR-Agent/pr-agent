import importlib
from decimal import Decimal
from pathlib import Path

import pytest

from pr_agent.algo.run_details import RunDetails
from pr_dashboard import store


class TestLazyDefaultPath:
    def test_import_never_calls_path_home(self, monkeypatch):
        """Reloading the module with a broken Path.home() must not raise -- it is import time"""
        def boom():
            raise RuntimeError("HOME is unset and this user has no passwd entry")

        monkeypatch.setattr(Path, "home", boom)
        importlib.reload(store)  # must not raise: the module body never calls Path.home()

    def test_default_db_path_is_only_computed_on_access(self, monkeypatch):
        """DEFAULT_DB_PATH still works as an attribute, computed lazily on that access"""
        def boom():
            raise RuntimeError("HOME is unset and this user has no passwd entry")

        monkeypatch.setattr(Path, "home", boom)
        with pytest.raises(RuntimeError):
            _ = store.DEFAULT_DB_PATH


class TestStoreSchema:
    def test_connect_creates_tables(self):
        """A fresh connection has the runs, run_model_costs and provider_cache tables"""
        conn = store.connect(":memory:")
        names = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"runs", "run_model_costs", "provider_cache", "ui_runs"} <= names

    def test_migrate_is_idempotent(self):
        """migrate() preserves existing data and schema across multiple invocations"""
        conn = store.connect(":memory:")
        # Capture schema before second migrate
        schema_before = conn.execute("PRAGMA table_info(runs)").fetchall()
        assert "dashboard_token" in {row["name"] for row in schema_before}
        # Insert rows to verify data preservation — a DROP-and-recreate would pass a
        # weaker "can call twice" check while destroying usage history.
        run_id = store.start_run(
            conn, provider="github", command="review", pr_url="https://github.com/o/r/pull/1",
            repo_slug="o/r", pr_number=1, started_at="2026-09-10T10:00:00Z",
        )
        store.start_ui_run(
            conn, token="tok-preserve", provider="github", repo_slug="o/r", pr_number=1,
            pr_url="https://github.com/o/r/pull/1", command="review",
            log_path="/tmp/tok-preserve.log", started_at="2026-09-10T10:00:00Z",
        )
        store.migrate(conn)
        row = store.get_run(conn, run_id)
        assert row is not None, "Data was lost during second migrate call"
        assert row["pr_number"] == 1
        ui = store.get_ui_run(conn, "tok-preserve")
        assert ui is not None, "ui_runs row was lost during second migrate call"
        assert ui["command"] == "review"
        schema_after = conn.execute("PRAGMA table_info(runs)").fetchall()
        assert schema_before == schema_after, "Schema changed during second migrate call"


class TestRunLifecycle:
    def test_start_run_records_an_attempt(self):
        """start_run inserts a running row carrying the identity columns"""
        conn = store.connect(":memory:")
        run_id = store.start_run(
            conn,
            provider="github",
            command="review",
            pr_url="https://github.com/o/r/pull/7",
            repo_slug="o/r",
            pr_number=7,
            started_at="2026-09-10T10:00:00Z",
        )
        row = store.get_run(conn, run_id)
        assert row["status"] == "running"
        assert row["repo_slug"] == "o/r"
        assert row["pr_number"] == 7
        assert row["total_tokens"] is None

    def test_finish_run_without_details_keeps_usage_null(self):
        """A run that never reached a tool is completed with an error and no usage"""
        conn = store.connect(":memory:")
        run_id = store.start_run(
            conn, provider="github", command="review", pr_url=None,
            repo_slug=None, pr_number=None, started_at="2026-09-10T10:00:00Z",
        )
        store.finish_run(
            conn, run_id, status="failed", finished_at="2026-09-10T10:00:01Z",
            details=None, error_text="bad url",
        )
        row = store.get_run(conn, run_id)
        assert row["status"] == "failed"
        assert row["error_text"] == "bad url"
        assert row["total_tokens"] is None
        assert store.run_cost(row) is None

    def test_finish_run_round_trips_decimal_cost(self):
        """Cost survives storage exactly, with no float rounding"""
        conn = store.connect(":memory:")
        details = RunDetails()
        details.model_used = "gemini/gemini-3.5-flash"
        details.prompt_tokens = 1200
        details.completion_tokens = 300
        details.total_tokens = 1500
        details.num_ai_calls = 2
        details.known_cost_call_count = 2
        details.total_cost_usd = Decimal("0.0000123456789")
        details.model_costs_usd = {"gemini/gemini-3.5-flash": Decimal("0.0000123456789")}

        run_id = store.start_run(
            conn, provider="github", command="review", pr_url="https://github.com/o/r/pull/7",
            repo_slug="o/r", pr_number=7, started_at="2026-09-10T10:00:00Z",
        )
        store.finish_run(conn, run_id, status="ok", finished_at="2026-09-10T10:00:09Z", details=details)

        row = store.get_run(conn, run_id)
        assert row["status"] == "ok"
        assert row["total_tokens"] == 1500
        assert row["cost_status"] == "complete"
        assert store.run_cost(row) == Decimal("0.0000123456789")
        costs = conn.execute("SELECT model, cost_usd FROM run_model_costs WHERE run_id = ?", (run_id,)).fetchall()
        assert [(r["model"], r["cost_usd"]) for r in costs] == [
            ("gemini/gemini-3.5-flash", "0.0000123456789")
        ]

    def test_unpriced_run_stores_no_cost(self):
        """A run litellm could not price stores NULL, never a misleading zero"""
        conn = store.connect(":memory:")
        details = RunDetails()
        details.model_used = "ollama/llama3"
        details.total_tokens = 900
        details.num_ai_calls = 1
        run_id = store.start_run(
            conn, provider="github", command="improve", pr_url=None,
            repo_slug="o/r", pr_number=7, started_at="2026-09-10T10:00:00Z",
        )
        store.finish_run(conn, run_id, status="ok", finished_at="2026-09-10T10:00:05Z", details=details)
        row = store.get_run(conn, run_id)
        assert row["cost_status"] == "unavailable"
        assert row["total_cost_usd"] is None
        assert store.run_cost(row) is None


class TestUiRuns:
    def test_start_ui_run_round_trips_every_field(self):
        """start_ui_run then get_ui_run returns every field that was written"""
        conn = store.connect(":memory:")
        store.start_ui_run(
            conn, token="abc-123", provider="github", repo_slug="o/r", pr_number=7,
            pr_url="https://github.com/o/r/pull/7", command="review",
            log_path="/tmp/abc-123.log", started_at="2026-09-11T12:00:00Z",
        )
        row = store.get_ui_run(conn, "abc-123")
        assert row is not None
        assert row["token"] == "abc-123"
        assert row["provider"] == "github"
        assert row["repo_slug"] == "o/r"
        assert row["pr_number"] == 7
        assert row["pr_url"] == "https://github.com/o/r/pull/7"
        assert row["command"] == "review"
        assert row["status"] == "queued"
        assert row["log_path"] == "/tmp/abc-123.log"
        assert row["started_at"] == "2026-09-11T12:00:00Z"
        assert row["pid"] is None
        assert row["exit_code"] is None
        assert row["finished_at"] is None

    def test_finish_ui_run_sets_status_and_exit_code(self):
        """finish_ui_run records terminal status, exit code, and finished_at"""
        conn = store.connect(":memory:")
        store.start_ui_run(
            conn, token="fin-1", provider="github", repo_slug="o/r", pr_number=1,
            pr_url="https://github.com/o/r/pull/1", command="improve",
            log_path="/tmp/fin-1.log", started_at="2026-09-11T12:00:00Z",
        )
        store.finish_ui_run(
            conn, token="fin-1", status="ok", exit_code=0, finished_at="2026-09-11T12:01:00Z")
        row = store.get_ui_run(conn, "fin-1")
        assert row["status"] == "ok"
        assert row["exit_code"] == 0
        assert row["finished_at"] == "2026-09-11T12:01:00Z"

    def test_list_ui_runs_newest_first_with_limit(self):
        """list_ui_runs returns newest first and honours the limit"""
        conn = store.connect(":memory:")
        for i, started in enumerate(("2026-09-11T10:00:00Z", "2026-09-11T11:00:00Z", "2026-09-11T12:00:00Z")):
            store.start_ui_run(
                conn, token=f"t{i}", provider="github", repo_slug="o/r", pr_number=i,
                pr_url=f"https://github.com/o/r/pull/{i}", command="review",
                log_path=f"/tmp/t{i}.log", started_at=started,
            )
        rows = store.list_ui_runs(conn, limit=2)
        assert [r["token"] for r in rows] == ["t2", "t1"]

    def test_null_dashboard_tokens_coexist_but_duplicates_do_not(self):
        """Many NULL dashboard_token values are allowed; a repeated non-NULL is not"""
        conn = store.connect(":memory:")
        for _ in range(2):
            store.start_run(
                conn, provider="github", command="review", pr_url=None,
                repo_slug="o/r", pr_number=1, started_at="2026-09-11T12:00:00Z",
            )
        nulls = conn.execute("SELECT count(*) AS n FROM runs WHERE dashboard_token IS NULL").fetchone()
        assert nulls["n"] == 2
        conn.execute("UPDATE runs SET dashboard_token = ? WHERE id = 1", ("dash-tok",))
        with pytest.raises(Exception):
            conn.execute("UPDATE runs SET dashboard_token = ? WHERE id = 2", ("dash-tok",))
        assert store.run_for_token(conn, "dash-tok")["id"] == 1
        assert store.run_for_token(conn, "missing") is None
