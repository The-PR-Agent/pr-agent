"""Privacy tests for the pr_agent.command span in pr_agent.agent.pr_agent.

Exception messages and rejected-command text are request content: they can
embed PR URLs, repo names, or anything the user typed. Exporting them must be
opt-in via OTEL.INCLUDE_ERROR_DETAILS (default off). The bounded values — the
span status and the error/exception *type* — are always safe to export.

Rejected (unknown) commands must also never become the span name, the
pr_agent.command attribute, or a pr_agent.commands metric label, and must not
inflate the counter.
"""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import Request
from openai import APIConnectionError
from opentelemetry.trace import StatusCode
from starlette_context import request_cycle_context

import pr_agent.agent.pr_agent as pr_agent_module
from pr_agent.agent.pr_agent import PRAgent
from pr_agent.algo.run_details import command_failed
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings
from tests.unittest._telemetry_helpers import build_in_memory_tracer

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
    monkeypatch.setattr(pr_agent_module, "enforce_request_policy", lambda _url: None)
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
    assert spans[0].name == "pr_agent.command", "arbitrary user input must not become the span name"
    assert spans[0].status.status_code is StatusCode.ERROR
    assert spans[0].status.description is None, "status description is free text — never request content"
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

    assert counter.calls == [], "unknown commands must not inflate pr_agent.commands"


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
    assert labels["vcs.provider.name"] == get_settings().config.git_provider


@pytest.mark.asyncio
@pytest.mark.parametrize("follow_up_filename", ["app.py", "ignored.py"])
async def test_recorded_review_failure_marks_span_and_next_review_starts_clean(
    follow_up_filename, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_on")
    monkeypatch.setenv("AUTO_CAST_FOR_DYNACONF", "false")
    tracer, exporter = build_in_memory_tracer()
    monkeypatch.setattr(pr_agent_module, "get_tracer", lambda: tracer)
    settings = copy.deepcopy(get_settings())
    for key, value in {
        "config.git_provider": "plain-diff",
        "config.model": "gpt-4o",
        "config.fallback_models": ["gpt-4o-mini"],
        "config.use_repo_settings_file": False,
        "config.extra_config_url": None,
        "config.propagate_tool_errors": False,
        "config.repo_context_files": [],
        "plain_diff.output_path": None,
        "plain_diff.json_output_path": None,
        "ignore.glob": ["ignored.py"],
        "ignore.regex": [],
        "pr_reviewer.persistent_comment": False,
        "pr_reviewer.enable_review_labels_effort": False,
        "pr_reviewer.enable_review_labels_security": False,
        "otel.include_error_details": False,
    }.items():
        settings.set(key, value)
    diff = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
"""
    response = (
        "review:\n  estimated_effort_to_review_[1-5]: 1\n"
        "  relevant_tests: 'No'\n  security_concerns: 'No'\n  key_issues_to_review: []\n",
        "stop",
    )
    for failed, filename in [(True, "app.py"), (False, follow_up_filename)]:
        ai = SimpleNamespace()
        ai.chat_completion = AsyncMock(
            side_effect=APIConnectionError(request=Request("POST", SENTINEL_URL)) if failed else None,
            return_value=response,
        )
        settings.set("plain_diff.content", diff.replace("app.py", filename))
        # Each request gets a fresh provider cache; the failure collector remains in this task.
        with request_cycle_context({"settings": settings}):
            handled = await PRAgent(ai_handler=lambda ai=ai: ai).handle_request("local_diff", "/review")

        assert handled is True
        assert command_failed() is failed
        expected_models = ["gpt-4o", "gpt-4o-mini"] if failed else (
            ["gpt-4o"] if filename == "app.py" else []
        )
        assert [call.kwargs["model"] for call in ai.chat_completion.await_args_list] == expected_models
        for call in ai.chat_completion.await_args_list:
            assert "value = 2" in call.kwargs["user"]
        span = exporter.get_finished_spans()[-1]
        assert span.status.status_code is (StatusCode.ERROR if failed else StatusCode.OK)
        assert span.attributes.get("error.type") == ("tool_execution_failed" if failed else None)
        assert span.status.description is None
        assert "error.message" not in span.attributes
        assert SENTINEL_URL not in str((span.attributes, span.status.description, span.events))
        assert span.events == ()

    assert len(exporter.get_finished_spans()) == 2


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

    def exploding(pr_url):
        raise RuntimeError(f"failed to fetch {SENTINEL_URL}")

    # Raised inside the span so the handler's own except-block records it.
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", exploding)

    handled = await PRAgent().handle_request("https://example/pr/1", "/review")

    assert handled is False
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code is StatusCode.ERROR
    assert spans[0].status.description is None, "status description is free text — never request content"
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
async def test_tool_exception_not_auto_recorded_by_default(telemetry, monkeypatch):
    """The OTel SDK auto-records exceptions (message, stacktrace, status
    description) on every span they propagate through — start_as_current_span
    defaults to record_exception=True. A failing tool must not smuggle its
    message onto the span that way, bypassing the error.message opt-in."""
    exporter, _ = telemetry
    monkeypatch.setitem(pr_agent_module.command2class, "customcmd", _ExplodingTool)

    handled = await PRAgent(ai_handler="fake").handle_request("https://example/pr/1", ["customcmd"])

    assert handled is False
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].events == (), "the command span must not auto-record the exception"
    assert spans[0].status.description is None
    assert SENTINEL_URL not in _all_exported_text(exporter)


@pytest.mark.asyncio
async def test_incomplete_files_error_keeps_bounded_telemetry_when_notification_fails(
    telemetry, monkeypatch
):
    exporter, _ = telemetry
    provider = Mock()
    provider.get_issue_comments_newest_first.return_value = []
    provider.supports_html_comment_markers.return_value = True
    provider.publish_comment.side_effect = RuntimeError("notification failed")

    class IncompleteTool:
        def __init__(self, pr_url, ai_handler, args):
            raise pr_agent_module.IncompletePullRequestFilesError(
                f"failed to fetch {SENTINEL_URL}"
            )

    monkeypatch.setattr(get_settings().config, "publish_output", True, raising=False)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _url: provider)
    monkeypatch.setitem(pr_agent_module.command2class, "customcmd", IncompleteTool)

    handled = await PRAgent(ai_handler="fake")._handle_request(
        "https://example/pr/1", ["customcmd"]
    )

    assert handled is False
    provider.publish_comment.assert_called_once()
    span = exporter.get_finished_spans()[0]
    assert dict(span.attributes)["error.type"] == "IncompletePullRequestFilesError"
    assert SENTINEL_URL not in _all_exported_text(exporter)


@pytest.mark.asyncio
async def test_incomplete_files_error_keeps_primary_telemetry_when_comment_decode_fails(
    telemetry, monkeypatch
):
    exporter, _ = telemetry
    provider = Mock()
    provider.get_issue_comments_newest_first.return_value = [object()]
    provider._get_comment_body.side_effect = RuntimeError(
        f"could not decode comment from {SENTINEL_URL}"
    )

    class IncompleteTool:
        def __init__(self, pr_url, ai_handler, args):
            raise pr_agent_module.IncompletePullRequestFilesError("primary failure")

    monkeypatch.setattr(get_settings().config, "publish_output", True, raising=False)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _url: provider)
    monkeypatch.setitem(pr_agent_module.command2class, "customcmd", IncompleteTool)

    handled = await PRAgent(ai_handler="fake")._handle_request(
        "https://example/pr/1", ["customcmd"]
    )

    assert handled is False
    provider.publish_comment.assert_called_once()
    span = exporter.get_finished_spans()[0]
    assert dict(span.attributes)["error.type"] == "IncompletePullRequestFilesError"
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

    def exploding(pr_url):
        raise RuntimeError(f"failed to fetch {SENTINEL_URL}")

    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", exploding)

    await PRAgent().handle_request("https://example/pr/1", "/review")

    span = exporter.get_finished_spans()[0]
    attrs = dict(span.attributes)
    assert SENTINEL_URL in attrs["error.message"]
    assert any(event.name == "exception" for event in span.events)
