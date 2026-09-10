import asyncio
import sqlite3

import pytest

from pr_agent.algo.run_details import init_run_details, record_ai_call, record_model_used
from pr_dashboard import recorder, store


class _Usage:
    prompt_tokens = 800
    completion_tokens = 200
    total_tokens = 1000


@pytest.fixture
def conn(tmp_path, monkeypatch):
    connection = store.connect(tmp_path / "usage.db")
    monkeypatch.setattr(recorder, "_open_store", lambda: connection)
    monkeypatch.setattr(recorder, "recording_enabled", lambda: True)
    return connection


class TestParsePrUrl:
    def test_github_url(self):
        """A GitHub pull request URL yields the slug and number"""
        assert recorder.parse_pr_url("https://github.com/samer2373/block_rush/pull/1") == (
            "samer2373/block_rush", 1)

    def test_bitbucket_url(self):
        """A Bitbucket pull request URL yields the slug and number"""
        assert recorder.parse_pr_url("https://bitbucket.org/team/svc/pull-requests/42") == (
            "team/svc", 42)

    def test_unparsable_url_is_not_fatal(self):
        """An unrecognised URL yields no identity rather than raising"""
        assert recorder.parse_pr_url("not a url") == (None, None)

    def test_none_url(self):
        """A missing URL yields no identity"""
        assert recorder.parse_pr_url(None) == (None, None)


class TestRecordRun:
    def test_records_usage_set_inside_the_body(self, conn):
        """RunDetails installed inside the with-body is visible when the block exits"""
        async def run_tool():
            init_run_details()
            # A real tool populates model_used via record_model_used (see
            # pr_agent/algo/pr_processing.py:360) separately from the per-call usage/cost
            # accounting in record_ai_call (see litellm_ai_handler.py:477); both are needed
            # here to stand in for a real completion.
            record_model_used("gemini/gemini-3.5-flash", is_fallback=False)
            record_ai_call(_Usage(), model="gemini/gemini-3.5-flash", cost_usd="0.0004")

        async def main():
            with recorder.record_run(pr_url="https://github.com/o/r/pull/7", command="review"):
                await run_tool()

        asyncio.run(main())

        row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert row["status"] == "ok"
        assert row["command"] == "review"
        assert row["repo_slug"] == "o/r"
        assert row["pr_number"] == 7
        assert row["total_tokens"] == 1000
        assert row["model_used"] == "gemini/gemini-3.5-flash"
        assert store.run_cost(row) is not None

    def test_records_an_attempt_when_no_collector_was_installed(self, conn):
        """A command that never calls init_run_details still leaves an auditable row"""
        with recorder.record_run(pr_url="https://github.com/o/r/pull/7", command="ask"):
            pass
        row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert row["status"] == "ok"
        assert row["total_tokens"] is None
        assert row["cost_status"] is None

    def test_records_failure_and_reraises(self, conn):
        """An exception is recorded and then propagates unchanged"""
        with pytest.raises(RuntimeError, match="boom"):
            with recorder.record_run(pr_url="https://github.com/o/r/pull/7", command="review"):
                raise RuntimeError("boom")
        row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert row["status"] == "failed"
        assert "boom" in row["error_text"]

    def test_disabled_recording_writes_nothing(self, tmp_path, monkeypatch):
        """With pr_dashboard.record_runs off, no database is opened at all"""
        monkeypatch.setattr(recorder, "recording_enabled", lambda: False)
        def explode():
            raise AssertionError("the store must not be opened when recording is disabled")
        monkeypatch.setattr(recorder, "_open_store", explode)
        with recorder.record_run(pr_url="https://github.com/o/r/pull/7", command="review"):
            pass

    def test_store_failure_never_breaks_the_run(self, monkeypatch):
        """A recorder error is swallowed, because a dashboard write must not fail a review"""
        monkeypatch.setattr(recorder, "recording_enabled", lambda: True)

        def explode():
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(recorder, "_open_store", explode)
        with recorder.record_run(pr_url="https://github.com/o/r/pull/7", command="review"):
            pass


class TestDispatchWiring:
    def test_pr_agent_wraps_dispatch_with_record_run(self):
        """pr_agent's dispatch is wrapped, and the tool is still awaited directly.

        The recorder relies on ContextVar propagation through a direct await. An upstream
        refactor to asyncio.gather or create_task around the tool would silently zero all
        usage, so assert the shape here rather than discovering it in the dashboard.

        _handle_request dispatches to _run_command, which holds the record_run(...) wrapper
        and the actual tool dispatch, so both are checked: _run_command for the wrapper and
        the direct await onto the tool, and _handle_request for the direct await onto
        _run_command (a gather/create_task on that hop would zero usage just as surely as
        one around the tool itself).
        """
        import inspect

        from pr_agent.agent.pr_agent import PRAgent

        source = inspect.getsource(PRAgent._run_command)
        assert "record_run(" in source
        dispatch_line = next(line for line in source.splitlines() if "command2class[action](" in line)
        assert dispatch_line.strip().startswith("await ")
        # Scoped to _run_command/_handle_request on purpose: the module legitimately uses
        # asyncio.to_thread elsewhere (flush_telemetry), so a whole-file assertion would be
        # brittle and would tempt a future reader to weaken it.
        assert "gather" not in source
        assert "create_task" not in source

        outer = inspect.getsource(PRAgent._handle_request)
        assert "await self._run_command(" in outer
        assert "gather" not in outer
        assert "create_task" not in outer
