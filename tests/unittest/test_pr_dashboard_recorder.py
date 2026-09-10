import asyncio
import sqlite3
from unittest.mock import MagicMock

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
        # A tracking stub, not an exploding one: record_run's own broad `except Exception`
        # (see test_store_failure_never_breaks_the_run) would swallow an AssertionError
        # raised from inside _open_store just as readily as a real store error, so a call
        # made and then caught would pass silently. Assert the absence of a call instead,
        # from the test body, after the with-block, where nothing in record_run can catch it.
        open_store = MagicMock()
        monkeypatch.setattr(recorder, "_open_store", open_store)
        with recorder.record_run(pr_url="https://github.com/o/r/pull/7", command="review"):
            pass
        open_store.assert_not_called()

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
        """pr_agent's dispatch is wrapped, and the tool is still awaited directly."""
        import inspect

        from pr_agent.agent.pr_agent import PRAgent

        # The recorder relies on ContextVar propagation through a direct await. An upstream
        # refactor to asyncio.gather or create_task around the tool would silently zero all
        # usage, so assert the shape here rather than discovering it in the dashboard.
        #
        # _handle_request dispatches to _run_command, which holds the record_run(...) wrapper
        # and the actual tool dispatch, so both are checked: _run_command for the wrapper and
        # the direct await onto the tool, and _handle_request for the direct await onto
        # _run_command (a gather/create_task on that hop would zero usage just as surely as
        # one around the tool itself).
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


@pytest.fixture
def dashboard_package_missing():
    """Simulate a distribution that ships pr_agent without pr_dashboard.

    pyproject.toml's ``[tool.setuptools.packages.find]`` includes only ``pr_agent*``, and
    every Dockerfile stage copies only ``pr_agent``, so ``pr_dashboard.recorder`` may
    genuinely be unimportable in a real install. This blocks that one import, forces a fresh
    import of ``pr_agent.agent.pr_agent`` against the block, and restores both modules to
    their real state afterwards so no other test observes the reload.
    """
    import importlib
    import sys

    class _BlockDashboardRecorder:
        def find_spec(self, name, path, target=None):
            if name == "pr_dashboard.recorder":
                raise ImportError("pr_dashboard.recorder is unavailable in this test")
            return None

    real_pr_agent_module = sys.modules.get("pr_agent.agent.pr_agent")
    real_recorder_module = sys.modules.get("pr_dashboard.recorder")
    blocker = _BlockDashboardRecorder()

    sys.modules.pop("pr_agent.agent.pr_agent", None)
    sys.modules.pop("pr_dashboard.recorder", None)
    sys.meta_path.insert(0, blocker)
    try:
        yield importlib.import_module("pr_agent.agent.pr_agent")
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.pop("pr_agent.agent.pr_agent", None)
        sys.modules.pop("pr_dashboard.recorder", None)
        if real_recorder_module is not None:
            sys.modules["pr_dashboard.recorder"] = real_recorder_module
        if real_pr_agent_module is not None:
            sys.modules["pr_agent.agent.pr_agent"] = real_pr_agent_module
            importlib.reload(real_pr_agent_module)
        else:
            importlib.import_module("pr_agent.agent.pr_agent")


class TestMissingDashboardPackage:
    def test_pr_agent_runs_without_pr_dashboard_installed(self, dashboard_package_missing):
        """A distribution without pr_dashboard installed still imports and dispatches."""
        reloaded = dashboard_package_missing
        # Confirms the guard's fallback was actually exercised, not that some stale cached
        # module slipped past the block.
        assert reloaded.record_run.__doc__ == (
            "No-op stand-in when pr_dashboard is not installed (wheel and Docker builds)."
        )
        with reloaded.record_run(pr_url="https://github.com/o/r/pull/7", command="review"):
            pass
