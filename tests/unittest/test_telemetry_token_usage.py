"""Per-command token and AI-call metrics on the built-in OpenTelemetry pipeline.

The counters carry only bounded labels (command, git provider, fallback flag, and the
token type), never the repo, PR URL, or prompt content, and zero values are skipped so
a provider that reports no usage adds no timeseries.
"""

import pytest

import pr_agent.agent.pr_agent as pr_agent_module
from pr_agent.algo.run_details import RunDetails, init_run_details, record_ai_call
from pr_agent.config_loader import get_settings
from tests.unittest._telemetry_helpers import build_in_memory_meter, build_in_memory_tracer


class _FakeTool:
    def __init__(self, pr_url, ai_handler, args):
        pass

    async def run(self):
        return None


def _details(**overrides) -> RunDetails:
    details = RunDetails()
    for key, value in overrides.items():
        setattr(details, key, value)
    return details


def _data_points(reader, metric_name):
    points = []
    for resource_metric in reader.get_metrics_data().resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                if metric.name == metric_name:
                    points.extend(metric.data.data_points)
    return points


def _token_values(reader):
    """Map (token type, fallback used) to the exported value."""
    return {
        (attrs["gen_ai.token.type"], attrs["pr_agent.fallback_used"]): point.value
        for point in _data_points(reader, "pr_agent.tokens")
        for attrs in (dict(point.attributes),)
    }


def _ai_call_values(reader):
    return {
        attrs["pr_agent.fallback_used"]: point.value
        for point in _data_points(reader, "pr_agent.ai_calls")
        for attrs in (dict(point.attributes),)
    }


@pytest.fixture
def telemetry(monkeypatch):
    """Route the module's counters to instruments on a local in-memory meter."""
    meter, reader = build_in_memory_meter()
    commands_counter = meter.create_counter("pr_agent.commands", unit="{command}")
    tokens_counter = meter.create_counter("pr_agent.tokens", unit="{token}")
    ai_calls_counter = meter.create_counter("pr_agent.ai_calls", unit="{call}")

    tracer, _ = build_in_memory_tracer()
    monkeypatch.setattr(pr_agent_module, "get_tracer", lambda: tracer)
    monkeypatch.setattr(pr_agent_module, "get_commands_counter", lambda: commands_counter)
    monkeypatch.setattr(pr_agent_module, "get_tokens_counter", lambda: tokens_counter)
    monkeypatch.setattr(pr_agent_module, "get_ai_calls_counter", lambda: ai_calls_counter)
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(pr_agent_module.CliArgs, "validate_user_args", lambda args: (True, None))
    monkeypatch.setattr(pr_agent_module, "update_settings_from_args", lambda args: args)
    monkeypatch.setitem(pr_agent_module.command2class, "customcmd", _FakeTool)
    return reader


@pytest.mark.asyncio
async def test_token_and_ai_call_metrics_exported_with_bounded_labels(telemetry, monkeypatch):
    reader = telemetry
    monkeypatch.setattr(
        pr_agent_module,
        "get_run_details",
        lambda: _details(
            prompt_tokens=100,
            completion_tokens=20,
            cache_read_tokens=5,
            cache_creation_tokens=7,
            num_ai_calls=3,
            fallback_used=True,
        ),
    )

    handled = await pr_agent_module.PRAgent(ai_handler="fake")._handle_request("https://example/pr/1", ["customcmd"])

    assert handled is True
    assert _token_values(reader) == {
        ("input", True): 100,
        ("output", True): 20,
        ("cache_read", True): 5,
        ("cache_creation", True): 7,
    }
    assert _ai_call_values(reader) == {True: 3}

    labels = dict(_data_points(reader, "pr_agent.tokens")[0].attributes)
    assert labels["pr_agent.command"] == "customcmd"
    assert labels["vcs.provider.name"] == get_settings().config.git_provider
    assert "pr_agent.pr_url" not in labels


@pytest.mark.asyncio
async def test_zero_values_are_skipped(telemetry, monkeypatch):
    reader = telemetry
    monkeypatch.setattr(
        pr_agent_module,
        "get_run_details",
        lambda: _details(
            prompt_tokens=0,
            completion_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            num_ai_calls=0,
        ),
    )

    handled = await pr_agent_module.PRAgent(ai_handler="fake")._handle_request("https://example/pr/1", ["customcmd"])

    assert handled is True
    assert _data_points(reader, "pr_agent.tokens") == []
    assert _data_points(reader, "pr_agent.ai_calls") == []


@pytest.mark.asyncio
async def test_nothing_recorded_when_run_details_is_none(telemetry, monkeypatch):
    reader = telemetry
    monkeypatch.setattr(pr_agent_module, "get_run_details", lambda: None)

    handled = await pr_agent_module.PRAgent(ai_handler="fake")._handle_request("https://example/pr/1", ["customcmd"])

    assert handled is True
    assert _data_points(reader, "pr_agent.tokens") == []
    assert _data_points(reader, "pr_agent.ai_calls") == []


@pytest.mark.asyncio
async def test_fallback_flag_is_recorded_on_a_separate_timeseries(telemetry, monkeypatch):
    reader = telemetry
    monkeypatch.setattr(
        pr_agent_module,
        "get_run_details",
        lambda: _details(prompt_tokens=42, num_ai_calls=1, fallback_used=False),
    )

    await pr_agent_module.PRAgent(ai_handler="fake")._handle_request("https://example/pr/1", ["customcmd"])

    assert _token_values(reader) == {("input", False): 42}
    assert _ai_call_values(reader) == {False: 1}


class _RecordingTool:
    """A tool that makes an AI call but never installs its own collector (e.g. /ask)."""

    prompt_tokens = 0
    completion_tokens = 0

    def __init__(self, pr_url, ai_handler, args):
        pass

    async def run(self):
        record_ai_call(
            usage={
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
            },
            model="recording-model",
        )
        return None


class _FirstUsageTool(_RecordingTool):
    """Mimics a tool such as /review that installs its own collector on entry."""

    prompt_tokens = 10

    async def run(self):
        init_run_details()
        return await super().run()


class _SecondUsageTool(_RecordingTool):
    completion_tokens = 5


def _token_values_by_command(reader):
    return {
        (dict(point.attributes)["pr_agent.command"], dict(point.attributes)["gen_ai.token.type"]): point.value
        for point in _data_points(reader, "pr_agent.tokens")
    }


def _ai_call_values_by_command(reader):
    return {
        dict(point.attributes)["pr_agent.command"]: point.value
        for point in _data_points(reader, "pr_agent.ai_calls")
    }


@pytest.mark.asyncio
async def test_command_without_its_own_collector_exports_its_usage(telemetry, monkeypatch):
    reader = telemetry
    monkeypatch.setitem(pr_agent_module.command2class, "usagecmd", _SecondUsageTool)

    handled = await pr_agent_module.PRAgent(ai_handler="fake")._handle_request(
        "https://example/pr/1", ["usagecmd"])

    assert handled is True
    assert _token_values_by_command(reader) == {("usagecmd", "output"): 5}
    assert _ai_call_values_by_command(reader) == {"usagecmd": 1}


@pytest.mark.asyncio
async def test_each_command_starts_from_a_fresh_collector(telemetry, monkeypatch):
    reader = telemetry
    monkeypatch.setitem(pr_agent_module.command2class, "firstcmd", _FirstUsageTool)
    monkeypatch.setitem(pr_agent_module.command2class, "secondcmd", _SecondUsageTool)
    agent = pr_agent_module.PRAgent(ai_handler="fake")

    await agent._handle_request("https://example/pr/1", ["firstcmd"])
    await agent._handle_request("https://example/pr/1", ["secondcmd"])

    # Without a fresh collector, secondcmd would inherit firstcmd's collector and repeat
    # its input tokens (and its AI-call count) under its own label instead of exporting
    # only its own output tokens.
    assert _token_values_by_command(reader) == {
        ("firstcmd", "input"): 10,
        ("secondcmd", "output"): 5,
    }
    assert _ai_call_values_by_command(reader) == {"firstcmd": 1, "secondcmd": 1}
