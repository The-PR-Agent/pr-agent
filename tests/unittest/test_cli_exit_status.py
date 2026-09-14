import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent import cli
from pr_agent.config_loader import get_settings

_CONSOLE_SCRIPT_FALLBACK = (
    "import sys; from pr_agent.cli import run; sys.exit(run())"
)
_SETTINGS_KEYS = [
    "config.propagate_tool_errors",
    "config.cli_mode",
    "config.config_branch",
    "config.extra_config_url",
]


@pytest.fixture(autouse=True)
def restore_cli_settings():
    settings = get_settings()
    original = {key: settings.get(key, None) for key in _SETTINGS_KEYS}
    yield
    for key, value in original.items():
        settings.set(key, value)


def _run_with_result(monkeypatch, result, *, propagate_tool_errors):
    fake_settings = SimpleNamespace(
        config={"propagate_tool_errors": propagate_tool_errors},
        litellm={},
        set=MagicMock(),
    )

    async def fake_handle_request(*_args, **_kwargs):
        return result

    monkeypatch.setattr(cli, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(
        cli,
        "PRAgent",
        lambda: SimpleNamespace(handle_request=fake_handle_request),
    )
    monkeypatch.setattr(cli, "inject_artifact_context", lambda: None)

    return cli.run(inargs=["--pr_url=https://example.com/org/repo/pull/1", "review"])


@pytest.mark.parametrize(
    ("request_result", "propagate_tool_errors", "expected_status", "prints_help"),
    [
        (True, False, None, False),
        (True, True, None, False),
        (False, False, None, True),
        (False, True, 1, True),
        (None, True, None, True),
        (0, True, None, True),
    ],
)
def test_run_maps_request_result_to_status(
    monkeypatch,
    capsys,
    request_result,
    propagate_tool_errors,
    expected_status,
    prints_help,
):
    status = _run_with_result(
        monkeypatch,
        request_result,
        propagate_tool_errors=propagate_tool_errors,
    )

    assert status == expected_status
    assert ("usage:" in capsys.readouterr().out) is prints_help


def test_run_reads_effective_setting_after_dispatch(monkeypatch):
    fake_settings = SimpleNamespace(
        config={"propagate_tool_errors": False},
        litellm={},
        set=MagicMock(),
    )

    async def fake_handle_request(*_args, **_kwargs):
        fake_settings.config["propagate_tool_errors"] = True
        return False

    monkeypatch.setattr(cli, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(
        cli,
        "PRAgent",
        lambda: SimpleNamespace(handle_request=fake_handle_request),
    )
    monkeypatch.setattr(cli, "inject_artifact_context", lambda: None)

    assert cli.run(inargs=["--pr_url=https://example.com/org/repo/pull/1", "review"]) == 1


def test_run_drains_callbacks_before_returning_failure_status(monkeypatch):
    events = []
    fake_settings = SimpleNamespace(
        config={"propagate_tool_errors": True},
        litellm={},
        set=MagicMock(),
    )

    async def fake_handle_request(*_args, **_kwargs):
        events.append("request")
        return False

    async def fake_drain(*_args, **_kwargs):
        events.append("drain")

    monkeypatch.setattr(cli, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(
        cli,
        "PRAgent",
        lambda: SimpleNamespace(handle_request=fake_handle_request),
    )
    monkeypatch.setattr(cli, "inject_artifact_context", lambda: None)
    monkeypatch.setattr(cli, "litellm_callbacks_registered", lambda: True)
    monkeypatch.setattr(cli, "drain_litellm_callbacks", fake_drain)

    status = cli.run(inargs=["--pr_url=https://example.com/org/repo/pull/1", "review"])

    events.append(f"status:{status}")
    assert events == ["request", "drain", "status:1"]


@pytest.mark.parametrize(
    ("repo_value", "cli_value", "expected_status"),
    [
        (False, True, 1),
        (True, None, 1),
        (True, False, None),
    ],
)
def test_real_request_failure_uses_effective_propagation_setting(
    monkeypatch,
    repo_value,
    cli_value,
    expected_status,
):
    from pr_agent.agent import pr_agent as pr_agent_module

    events = []
    expected_effective_value = repo_value if cli_value is None else cli_value

    class FailingReview:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self):
            events.append("tool")
            assert (
                get_settings().config.get("propagate_tool_errors")
                is expected_effective_value
            )
            raise RuntimeError("controlled tool failure")

    async def fake_drain(*_args, **_kwargs):
        events.append("callbacks")

    def fake_apply_repo_settings(*_args, **_kwargs):
        get_settings().set("CONFIG.PROPAGATE_TOOL_ERRORS", repo_value)

    monkeypatch.setitem(pr_agent_module.command2class, "review", FailingReview)
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", fake_apply_repo_settings)
    monkeypatch.setattr(pr_agent_module, "flush_telemetry", lambda: events.append("telemetry"))
    monkeypatch.setattr(cli, "inject_artifact_context", lambda: None)
    monkeypatch.setattr(cli, "litellm_callbacks_registered", lambda: True)
    monkeypatch.setattr(cli, "drain_litellm_callbacks", fake_drain)

    inargs = [
        "--pr_url=https://example.com/org/repo/pull/1",
        "review",
    ]
    if cli_value is not None:
        inargs.append(f"--config.propagate_tool_errors={str(cli_value).lower()}")

    status = cli.run(inargs=inargs)

    events.append(f"status:{status}")
    assert events == [
        "tool",
        "telemetry",
        "callbacks",
        f"status:{expected_status}",
    ]


@pytest.mark.parametrize(
    ("baseline", "override_source", "override", "first_succeeds", "expected_statuses"),
    [
        (False, "repository", True, False, [1, None]),
        (False, "command", True, False, [1, None]),
        (True, "repository", False, False, [None, 1]),
        (True, "command", False, False, [None, 1]),
        (False, "repository", True, True, [None, None]),
        (False, "command", True, True, [None, None]),
        (True, "repository", False, True, [None, 1]),
        (True, "command", False, True, [None, 1]),
    ],
)
def test_run_restores_propagation_setting_between_invocations(
    monkeypatch,
    baseline,
    override_source,
    override,
    first_succeeds,
    expected_statuses,
):
    from pr_agent.agent import pr_agent as pr_agent_module

    settings = get_settings()
    settings.set("CONFIG.PROPAGATE_TOOL_ERRORS", baseline)
    apply_calls = 0
    observed_values = []
    request_results = iter([first_succeeds, False])

    class ControlledReview:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self):
            observed_values.append(settings.config.get("propagate_tool_errors"))
            if next(request_results):
                return
            raise RuntimeError("controlled tool failure")

    def fake_apply_repo_settings(*_args, **_kwargs):
        nonlocal apply_calls
        if override_source == "repository" and apply_calls == 0:
            settings.set("CONFIG.PROPAGATE_TOOL_ERRORS", override)
        apply_calls += 1

    monkeypatch.setitem(pr_agent_module.command2class, "review", ControlledReview)
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", fake_apply_repo_settings)
    monkeypatch.setattr(pr_agent_module, "flush_telemetry", lambda: None)
    monkeypatch.setattr(cli, "inject_artifact_context", lambda: None)
    monkeypatch.setattr(cli, "litellm_callbacks_registered", lambda: False)

    first_inargs = ["--pr_url=https://example.com/org/repo/pull/1", "review"]
    if override_source == "command":
        first_inargs.append(f"--config.propagate_tool_errors={str(override).lower()}")

    statuses = [
        cli.run(inargs=first_inargs),
        cli.run(inargs=["--pr_url=https://example.com/org/repo/pull/2", "review"]),
    ]

    assert observed_values == [override, baseline]
    assert statuses == expected_statuses
    assert settings.config.get("propagate_tool_errors") is baseline


def test_run_restores_propagation_setting_when_callback_drain_raises(monkeypatch):
    settings = get_settings()
    settings.set("CONFIG.PROPAGATE_TOOL_ERRORS", False)

    async def fake_handle_request(*_args, **_kwargs):
        settings.set("CONFIG.PROPAGATE_TOOL_ERRORS", True)
        return False

    async def failing_drain(*_args, **_kwargs):
        raise RuntimeError("controlled callback failure")

    monkeypatch.setattr(
        cli,
        "PRAgent",
        lambda: SimpleNamespace(handle_request=fake_handle_request),
    )
    monkeypatch.setattr(cli, "inject_artifact_context", lambda: None)
    monkeypatch.setattr(cli, "litellm_callbacks_registered", lambda: True)
    monkeypatch.setattr(cli, "drain_litellm_callbacks", failing_drain)

    with pytest.raises(RuntimeError, match="controlled callback failure"):
        cli.run(inargs=["--pr_url=https://example.com/org/repo/pull/1", "review"])

    assert settings.config.get("propagate_tool_errors") is False


def _console_script_entrypoint(python_executable):
    console_script = Path(python_executable).with_name("pr-agent")
    if sys.platform == "win32":
        console_script = console_script.with_suffix(".exe")
    if console_script.is_file():
        return [str(console_script)]
    return [python_executable, "-c", _CONSOLE_SCRIPT_FALLBACK]


def test_console_script_maps_to_cli_run():
    with (Path(__file__).parents[2] / "pyproject.toml").open("rb") as pyproject:
        project = tomllib.load(pyproject)["project"]

    assert project["scripts"]["pr-agent"] == "pr_agent.cli:run"


def test_console_script_falls_back_in_source_only_environment(tmp_path):
    python_executable = str(tmp_path / "bin" / "python")
    assert _console_script_entrypoint(python_executable) == [
        python_executable,
        "-c",
        _CONSOLE_SCRIPT_FALLBACK,
    ]


def test_run_command_forwards_run_status(monkeypatch):
    def fake_run(*, args):
        assert args.pr_url == "https://example.com/org/repo/pull/1"
        assert args.command == "review"
        return 1

    monkeypatch.setattr(cli, "run", fake_run)

    assert cli.run_command("https://example.com/org/repo/pull/1", "/review") == 1


@pytest.fixture
def process_entrypoints():
    return (
        _console_script_entrypoint(sys.executable),
        [sys.executable, "-m", "pr_agent.cli"],
        [sys.executable, "-m", "pr_agent.cli_pip"],
        [sys.executable, "pr_agent/cli_pip.py"],
    )


@pytest.fixture
def argparse_entrypoints(process_entrypoints):
    return process_entrypoints[:2]


@pytest.fixture
def controlled_process_env(tmp_path):
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        """
import os

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.config_loader import get_settings


async def controlled_handle_request(self, pr_url, request, notify=None):
    get_settings().set(
        \"CONFIG.PROPAGATE_TOOL_ERRORS\",
        os.environ.get(\"PR_AGENT_TEST_PROPAGATE\") == \"true\",
    )
    result = os.environ.get(\"PR_AGENT_TEST_RESULT\")
    if result == \"none\":
        return None
    if result == \"zero\":
        return 0
    return result == \"true\"


PRAgent.handle_request = controlled_handle_request
""".lstrip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(tmp_path), env.get("PYTHONPATH")) if part
    )
    return env


@pytest.mark.parametrize(
    ("request_result", "propagate_tool_errors", "expected_status"),
    [
        ("false", "true", 1),
        ("false", "false", 0),
    ],
)
def test_process_entrypoints_map_request_status(
    process_entrypoints,
    controlled_process_env,
    request_result,
    propagate_tool_errors,
    expected_status,
):
    env = controlled_process_env.copy()
    env["PR_AGENT_TEST_RESULT"] = request_result
    env["PR_AGENT_TEST_PROPAGATE"] = propagate_tool_errors

    for entrypoint in process_entrypoints:
        completed = subprocess.run(
            [*entrypoint, "--pr_url=https://example.com/org/repo/pull/1", "review"],
            cwd=Path(__file__).parents[2],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        assert completed.returncode == expected_status, (
            entrypoint,
            completed.stdout,
            completed.stderr,
        )


def test_process_entrypoints_preserve_argparse_status(
    argparse_entrypoints,
    controlled_process_env,
):
    for entrypoint in argparse_entrypoints:
        completed = subprocess.run(
            [*entrypoint, "not-a-command"],
            cwd=Path(__file__).parents[2],
            env=controlled_process_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        assert completed.returncode == 2, (
            entrypoint,
            completed.stdout,
            completed.stderr,
        )
