"""Tests for PRAgentExecutor.execute (A2A 1.0).

Drives execute() with a fake RequestContext + a recording EventQueue, and a spy
TaskUpdater that captures add_artifact()/complete()/failed() calls. asyncio_mode=strict.

Non-vacuity (Fix C): test_non_vacuity_ok_false_must_not_complete verifies that if
ok=False causes complete() instead of failed(), the assertion fails — proving the
test can detect a Fix C regression."""
import pytest
from a2a.types import Message, Part, Role
from starlette_context import request_cycle_context

import pr_agent.mosaico.executor as executor_mod
from pr_agent.mosaico.dispatch import RouteResult
from pr_agent.mosaico.executor import PRAgentExecutor


def _make_message(text: str) -> Message:
    return Message(
        message_id="m1",
        role=Role.ROLE_USER,
        parts=[Part(text=text)],
    )


class _RecordingEventQueue:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


class _FakeRequestContext:
    """Faithful stand-in for a2a RequestContext: top-level metadata, get_user_input,
    task_id, context_id, message."""

    def __init__(self, text, metadata=None):
        self._text = text
        self.metadata = metadata or {}
        self.message = _make_message(text)
        # A2A 1.0: task_id/context_id are set by DefaultRequestHandler before execute.
        self.task_id = "task-001"
        self.context_id = "ctx-001"

    def get_user_input(self, delimiter: str = "\n") -> str:
        return self._text


class _SpyTaskUpdater:
    """Spy replacing TaskUpdater; captures add_artifact/complete/failed calls."""

    last = None

    def __init__(self, event_queue, task_id, context_id):
        self.event_queue = event_queue
        self.task_id = task_id
        self.context_id = context_id
        self.artifacts = []       # list of Part lists passed to add_artifact
        self.completed = False
        self.failed_with = None   # message text passed to failed()
        type(self).last = self

    async def add_artifact(self, parts, **kwargs):
        self.artifacts.append(parts)

    async def complete(self, message=None):
        self.completed = True

    async def failed(self, message=None):
        self.failed_with = _message_text(message)

    def new_agent_message(self, parts, metadata=None):
        return _FakeMessage(parts)


class _FakeMessage:
    def __init__(self, parts):
        self._parts = parts

    @property
    def parts(self):
        return self._parts


def _message_text(message):
    """Extract plain text from a message (real protobuf Part or FakeMessage)."""
    if message is None:
        return None
    try:
        parts = message.parts
        if parts:
            p = parts[0]
            if hasattr(p, "text"):
                return p.text
        return str(message)
    except Exception:
        return str(message)


def _artifact_text(spy):
    """Return the first text from the first artifact list, or None."""
    if not spy.artifacts:
        return None
    parts = spy.artifacts[0]
    if parts:
        p = parts[0]
        if hasattr(p, "text"):
            return p.text
    return None


@pytest.fixture
def spy_updater(monkeypatch):
    _SpyTaskUpdater.last = None
    monkeypatch.setattr(executor_mod, "TaskUpdater", _SpyTaskUpdater)
    return _SpyTaskUpdater


class TestExecute:
    @pytest.mark.asyncio
    async def test_completes_with_artifact(self, monkeypatch, spy_updater):
        """ok=True path: result goes into add_artifact (RISK 2), then complete()."""
        async def fake_route_and_run_result(text):
            return RouteResult("RENDERED", True)

        monkeypatch.setattr(executor_mod, "route_and_run_result", fake_route_and_run_result)

        eq = _RecordingEventQueue()
        ctx = _FakeRequestContext("review this", metadata={})
        with request_cycle_context({}):
            await PRAgentExecutor().execute(ctx, eq)

        spy = spy_updater.last
        # The review text must be in the artifact (RISK 2: reference agent reads artifacts).
        assert _artifact_text(spy) == "RENDERED"
        assert spy.completed is True
        assert spy.failed_with is None

    @pytest.mark.asyncio
    async def test_empty_render_artifact_has_placeholder(self, monkeypatch, spy_updater):
        async def fake_route_and_run_result(text):
            return RouteResult("", True)

        monkeypatch.setattr(executor_mod, "route_and_run_result", fake_route_and_run_result)
        with request_cycle_context({}):
            await PRAgentExecutor().execute(_FakeRequestContext("x"), _RecordingEventQueue())

        spy = spy_updater.last
        assert _artifact_text(spy) == "(no output produced)"
        assert spy.completed is True

    @pytest.mark.asyncio
    async def test_route_and_run_raises_marks_failed(self, monkeypatch, spy_updater):
        async def boom(text):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(executor_mod, "route_and_run_result", boom)

        eq = _RecordingEventQueue()
        with request_cycle_context({}):
            await PRAgentExecutor().execute(_FakeRequestContext("y"), eq)

        spy = spy_updater.last
        # Exception path: artifact is added (to init task), then failed().
        assert spy.artifacts, "artifact must be added even on exception path"
        assert spy.completed is False
        assert spy.failed_with is not None
        assert "kaboom" in spy.failed_with

    @pytest.mark.asyncio
    async def test_failed_result_marks_failed_not_completed(self, monkeypatch, spy_updater):
        """ok=False path (Fix C): artifact is added, then failed() — never complete()."""
        async def fake_route_and_run_result(text):
            return RouteResult("boom", ok=False)

        monkeypatch.setattr(executor_mod, "route_and_run_result", fake_route_and_run_result)

        with request_cycle_context({}):
            await PRAgentExecutor().execute(_FakeRequestContext("z"), _RecordingEventQueue())

        spy = spy_updater.last
        assert spy.artifacts, "artifact must be added before failed()"
        assert spy.failed_with is not None
        assert spy.completed is False

    @pytest.mark.asyncio
    async def test_non_vacuity_ok_false_must_not_complete(self, monkeypatch, spy_updater):
        """Non-vacuity guard (Fix C): if ok=False route calls complete() instead of
        failed(), this assertion fails — proving the test catches a Fix C regression."""
        async def bad_url_result(text):
            return RouteResult("SSRF blocked", ok=False)

        monkeypatch.setattr(executor_mod, "route_and_run_result", bad_url_result)

        with request_cycle_context({}):
            await PRAgentExecutor().execute(_FakeRequestContext("z"), _RecordingEventQueue())

        spy = spy_updater.last
        # If Fix C is reverted (complete() called on ok=False), this assertion fails.
        assert spy.completed is False, (
            "ok=False path must call failed(), not complete() — Fix C guard"
        )
        assert spy.failed_with is not None

    @pytest.mark.parametrize("missing", ["task_id", "context_id"])
    @pytest.mark.asyncio
    async def test_missing_a2a_fields_raise_controlled_error(self, monkeypatch, spy_updater, missing):
        """Defense in depth: a RequestContext lacking the A2A 1.0 task_id/context_id
        fields raises a controlled error (logged) before any TaskUpdater is built —
        not an uncaught crash, and never a silent complete()."""
        async def fake_route_and_run_result(text):
            return RouteResult("RENDERED", True)

        monkeypatch.setattr(executor_mod, "route_and_run_result", fake_route_and_run_result)

        ctx = _FakeRequestContext("review this")
        setattr(ctx, missing, None)
        with request_cycle_context({}):
            with pytest.raises(ValueError):
                await PRAgentExecutor().execute(ctx, _RecordingEventQueue())

        # TaskUpdater was never constructed (validation fires first), so no task
        # lifecycle events were emitted.
        assert spy_updater.last is None

    @pytest.mark.asyncio
    async def test_cancel_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            await PRAgentExecutor().cancel(_FakeRequestContext("z"), _RecordingEventQueue())
