"""Privacy tests for the telemetry spans in pr_agent.agent.pr_agent.

Exception messages and rejected-command text are request content: they can
embed PR URLs, repo names, or anything the user typed. Exporting them must be
opt-in via OTEL.INCLUDE_ERROR_DETAILS (default off). The bounded values —
error flag and error/exception *type* — are always safe to export.

Rejected (unknown) commands must also never become the pr_agent.command span
attribute or a pr_agent.commands.total metric label, and must not inflate the
counter.
"""

import pytest

import pr_agent.agent.pr_agent as pr_agent_module
from pr_agent.agent.pr_agent import PRAgent
from tests.unittest._settings_helpers import restore_settings, snapshot_settings
from tests.unittest._telemetry_helpers import build_in_memory_tracer
from pr_agent.config_loader import get_settings

SENTINEL_URL = "https://secret-repo.example/org/private-repo/pull/42"

_TRACKED_KEYS = ("otel.include_error_details",)


@pytest.fixture(autouse=True)
def _restore_otel_settings():
    snapshot = snapshot_settings(_TRACKED_KEYS)
    try:
        yield
    finally:
        restore_settings(snapshot)


class _RecordingCounter:
    def __init__(self):
        self.calls = []

    def add(self, amount, attributes=None):
        self.calls.append((amount, attributes))


@pytest.fixture
def telemetry(monkeypatch):
    """Route the module's tracer/counter to inspectable in-memory fakes."""
    tracer, exporter = build_in_memory_tracer()
    counter = _RecordingCounter()
    monkeypatch.setattr(pr_agent_module, "get_tracer", lambda: tracer)
    monkeypatch.setattr(pr_agent_module, "get_commands_counter", lambda: counter)
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(pr_agent_module.CliArgs, "validate_user_args", lambda args: (True, None))
    monkeypatch.setattr(pr_agent_module, "update_settings_from_args", lambda args: args)
    return exporter, counter


def _all_exported_text(exporter):
    return "".join(
        repr(dict(s.attributes)) + repr(s.events) + s.name + repr(s.status.description)
        for s in exporter.get_finished_spans()
    )


@pytest.mark.asyncio
async def test_unknown_command_text_not_exported_by_default(telemetry):
    exporter, counter = telemetry

    handled = await PRAgent()._handle_request("https://example/pr/1", [SENTINEL_URL])

    assert handled is False
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs["error.type"] == "unknown_command"
    assert "error.message" not in attrs, "rejected-command text must be opt-in"
    assert "pr_agent.command" not in attrs, "arbitrary user input must not become an attribute"
    assert SENTINEL_URL not in _all_exported_text(exporter)


@pytest.mark.asyncio
async def test_unknown_command_text_exported_when_opted_in(telemetry):
    exporter, _ = telemetry
    get_settings().set("otel.include_error_details", True)

    await PRAgent()._handle_request("https://example/pr/1", [SENTINEL_URL])

    attrs = dict(exporter.get_finished_spans()[0].attributes)
    assert SENTINEL_URL in attrs["error.message"]


@pytest.mark.asyncio
async def test_rejected_requests_do_not_increment_commands_counter(telemetry):
    _, counter = telemetry

    await PRAgent()._handle_request("https://example/pr/1", [SENTINEL_URL])

    assert counter.calls == [], "unknown commands must not inflate pr_agent.commands.total"


@pytest.mark.asyncio
async def test_known_command_increments_counter_with_bounded_labels(telemetry, monkeypatch):
    _, counter = telemetry

    class FakeTool:
        def __init__(self, pr_url, ai_handler, args):
            pass

        async def run(self):
            return None

    monkeypatch.setitem(pr_agent_module.command2class, "customcmd", FakeTool)

    handled = await PRAgent(ai_handler="fake")._handle_request("https://example/pr/1", ["customcmd"])

    assert handled is True
    assert len(counter.calls) == 1
    amount, labels = counter.calls[0]
    assert amount == 1
    assert labels["pr_agent.command"] == "customcmd"


@pytest.mark.asyncio
async def test_handle_request_flushes_telemetry_even_on_failure(telemetry, monkeypatch):
    """Serverless environments freeze right after the response, so every
    request — including failing ones — must end with a telemetry flush."""
    flushes = []
    monkeypatch.setattr(pr_agent_module, "flush_telemetry", lambda: flushes.append(True))

    async def exploding(self, pr_url, request, notify=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(PRAgent, "_handle_request", exploding)
    await PRAgent().handle_request("https://example/pr/1", "/review")
    assert flushes == [True], "failed requests must still flush telemetry"

    async def ok(self, pr_url, request, notify=None):
        return True

    monkeypatch.setattr(PRAgent, "_handle_request", ok)
    await PRAgent().handle_request("https://example/pr/1", "/review")
    assert len(flushes) == 2, "successful requests must flush telemetry too"


@pytest.mark.asyncio
async def test_exception_message_not_exported_by_default(telemetry, monkeypatch):
    exporter, _ = telemetry

    async def exploding(self, pr_url, request, notify=None):
        raise RuntimeError(f"failed to fetch {SENTINEL_URL}")

    monkeypatch.setattr(PRAgent, "_handle_request", exploding)

    handled = await PRAgent().handle_request("https://example/pr/1", "/review")

    assert handled is False
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs["error.type"] == "RuntimeError", "exception class name is bounded and stays"
    assert "error.message" not in attrs, "exception text must be opt-in"
    assert spans[0].events == (), "record_exception must be opt-in (event carries the message)"
    assert SENTINEL_URL not in _all_exported_text(exporter)


class _ExplodingTool:
    def __init__(self, pr_url, ai_handler, args):
        pass

    async def run(self):
        raise RuntimeError(f"failed to fetch {SENTINEL_URL}")


@pytest.mark.asyncio
async def test_propagating_exception_not_auto_recorded_on_inner_spans(telemetry, monkeypatch):
    """The OTel SDK auto-records exceptions (message, stacktrace, status
    description) on every span they propagate through — start_as_current_span
    defaults to record_exception=True. A tool failure escapes both inner spans
    before handle_request catches it, so those spans must gate the auto-record
    behind the same opt-in as the explicit error.message attribute."""
    exporter, _ = telemetry
    monkeypatch.setitem(pr_agent_module.command2class, "customcmd", _ExplodingTool)

    handled = await PRAgent(ai_handler="fake").handle_request("https://example/pr/1", ["customcmd"])

    assert handled is False
    spans = exporter.get_finished_spans()
    assert len(spans) == 3, "expected execute, handle_request, and request spans"
    for span in spans:
        assert span.events == (), f"{span.name} must not auto-record the exception"
    assert SENTINEL_URL not in _all_exported_text(exporter)


@pytest.mark.asyncio
async def test_propagating_exception_recorded_when_opted_in(telemetry, monkeypatch):
    exporter, _ = telemetry
    get_settings().set("otel.include_error_details", True)
    monkeypatch.setitem(pr_agent_module.command2class, "customcmd", _ExplodingTool)

    await PRAgent(ai_handler="fake").handle_request("https://example/pr/1", ["customcmd"])

    exception_events = [
        event for span in exporter.get_finished_spans() for event in span.events
        if event.name == "exception"
    ]
    assert exception_events, "opted-in deployments keep full exception fidelity"
    assert SENTINEL_URL in _all_exported_text(exporter)


@pytest.mark.asyncio
async def test_exception_details_exported_when_opted_in(telemetry, monkeypatch):
    exporter, _ = telemetry
    get_settings().set("otel.include_error_details", True)

    async def exploding(self, pr_url, request, notify=None):
        raise RuntimeError(f"failed to fetch {SENTINEL_URL}")

    monkeypatch.setattr(PRAgent, "_handle_request", exploding)

    await PRAgent().handle_request("https://example/pr/1", "/review")

    span = exporter.get_finished_spans()[0]
    attrs = dict(span.attributes)
    assert SENTINEL_URL in attrs["error.message"]
    assert any(event.name == "exception" for event in span.events)
