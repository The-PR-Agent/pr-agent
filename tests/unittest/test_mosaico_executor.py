"""Tests for PRAgentExecutor.execute.

Drives execute() with a fake RequestContext + a recording EventQueue, and a spy
TaskUpdater that captures complete()/failed() calls. asyncio_mode=auto."""
import pytest
from a2a.types import Message, Part, Role, TextPart
from starlette_context import request_cycle_context

import pr_agent.mosaico.executor as executor_mod
from pr_agent.mosaico.executor import PRAgentExecutor


def _make_message(text: str) -> Message:
    return Message(
        message_id="m1",
        role=Role.user,
        parts=[Part(root=TextPart(text=text))],
    )


class _RecordingEventQueue:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


class _FakeRequestContext:
    """Faithful stand-in for a2a RequestContext: top-level metadata, get_user_input,
    current_task, message."""

    def __init__(self, text, metadata=None, current_task=None):
        self._text = text
        self.metadata = metadata or {}
        self.current_task = current_task
        self.message = _make_message(text)

    def get_user_input(self, delimiter: str = "\n") -> str:
        return self._text


class _SpyTaskUpdater:
    last = None

    def __init__(self, event_queue, task_id, context_id):
        self.event_queue = event_queue
        self.task_id = task_id
        self.context_id = context_id
        self.completed_with = None
        self.failed_with = None
        type(self).last = self

    async def complete(self, message=None):
        self.completed_with = _message_text(message)

    async def failed(self, message=None):
        self.failed_with = _message_text(message)


def _message_text(message):
    if message is None:
        return None
    try:
        return message.parts[0].root.text
    except Exception:
        return str(message)


@pytest.fixture
def spy_updater(monkeypatch):
    _SpyTaskUpdater.last = None
    monkeypatch.setattr(executor_mod, "TaskUpdater", _SpyTaskUpdater)
    return _SpyTaskUpdater


class TestExecute:
    @pytest.mark.asyncio
    async def test_completes_with_rendered_markdown(self, monkeypatch, spy_updater):
        async def fake_route_and_run(text):
            return "RENDERED"

        monkeypatch.setattr(executor_mod, "route_and_run", fake_route_and_run)

        eq = _RecordingEventQueue()
        ctx = _FakeRequestContext("review this", metadata={})
        with request_cycle_context({}):
            await PRAgentExecutor().execute(ctx, eq)

        # The new Task was enqueued (current_task was None).
        assert len(eq.events) == 1
        assert spy_updater.last.completed_with == "RENDERED"
        assert spy_updater.last.failed_with is None

    @pytest.mark.asyncio
    async def test_empty_render_uses_placeholder(self, monkeypatch, spy_updater):
        async def fake_route_and_run(text):
            return ""

        monkeypatch.setattr(executor_mod, "route_and_run", fake_route_and_run)
        with request_cycle_context({}):
            await PRAgentExecutor().execute(_FakeRequestContext("x"), _RecordingEventQueue())
        assert spy_updater.last.completed_with == "(no output produced)"

    @pytest.mark.asyncio
    async def test_route_and_run_raises_marks_failed(self, monkeypatch, spy_updater):
        async def boom(text):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(executor_mod, "route_and_run", boom)

        eq = _RecordingEventQueue()
        # Must not raise out of execute.
        with request_cycle_context({}):
            await PRAgentExecutor().execute(_FakeRequestContext("y"), eq)
        assert spy_updater.last.completed_with is None
        assert spy_updater.last.failed_with is not None
        assert "kaboom" in spy_updater.last.failed_with

    @pytest.mark.asyncio
    async def test_existing_task_not_re_enqueued(self, monkeypatch, spy_updater):
        async def fake_route_and_run(text):
            return "R"

        monkeypatch.setattr(executor_mod, "route_and_run", fake_route_and_run)

        from a2a.utils import new_task
        existing = new_task(_make_message("hi"))
        eq = _RecordingEventQueue()
        ctx = _FakeRequestContext("hi", current_task=existing)
        with request_cycle_context({}):
            await PRAgentExecutor().execute(ctx, eq)
        # current_task present -> no new Task enqueued
        assert eq.events == []
        assert spy_updater.last.completed_with == "R"

    @pytest.mark.asyncio
    async def test_cancel_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            await PRAgentExecutor().cancel(_FakeRequestContext("z"), _RecordingEventQueue())
