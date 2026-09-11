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
        assert {"runs", "run_model_costs", "provider_cache"} <= names

    def test_migrate_is_idempotent(self):
        """migrate() preserves existing data and schema across multiple invocations"""
        conn = store.connect(":memory:")
        # Capture schema before second migrate
        schema_before = conn.execute("PRAGMA table_info(runs)").fetchall()
        # Insert a row to verify data preservation
        run_id = store.start_run(
            conn, provider="github", command="review", pr_url="https://github.com/o/r/pull/1",
            repo_slug="o/r", pr_number=1, started_at="2026-09-10T10:00:00Z",
        )
        # Call migrate a second time (should be idempotent)
        store.migrate(conn)
        # Verify row survived
        row = store.get_run(conn, run_id)
        assert row is not None, "Data was lost during second migrate call"
        assert row["pr_number"] == 1
        # Verify schema is unchanged
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
