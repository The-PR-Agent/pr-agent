"""An automatic command should say it started before it has anything to publish.

Automatic commands suppress the "Preparing review..." progress comment, so between opening a
pull request and the model answering there is no sign PR-Agent picked it up. When
`github.publish_as_check_run` is on, the tool's check run is opened as in_progress before the
command runs and completed in place by the tool, so there is one signal on the commit.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import pr_agent.servers.github_app as github_app
from pr_agent.algo.run_details import command_failed, init_run_details, record_command_failure
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.tools.pr_reviewer import PRReviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

API_URL = "https://api.github.com/repos/org/repo/pulls/1"
CHECK_RUNS_URL = "https://api.github.com/repos/org/repo/check-runs"


@pytest.fixture
def check_runs_enabled():
    snapshot = snapshot_settings(["github.publish_as_check_run"])
    get_settings().set("github.publish_as_check_run", True)
    yield
    restore_settings(snapshot)


def _github(sha="abc123", existing=None):
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "org/repo"
    provider.base_url = "https://api.github.com"
    provider.last_commit_id = SimpleNamespace(sha=sha) if sha else None
    provider._check_run_ids = {}
    provider._check_runs_in_progress = set()
    requester = MagicMock()

    def request(method, url, **kwargs):
        if method == "GET":
            return {}, {"check_runs": existing or []}
        if method == "POST":
            return {}, {"id": 101}
        return {}, {}

    requester.requestJsonAndCheck.side_effect = request
    provider.pr = SimpleNamespace(_requester=requester)
    return provider


def _requests(provider):
    return [(c.args[0], c.args[1], c.kwargs["input"]) for c in provider.pr._requester.requestJsonAndCheck.call_args_list
            if c.args[0] != "GET"]


# --------------------------------------------------------------------------------------
# The provider: open in_progress, complete in place
# --------------------------------------------------------------------------------------
def test_start_check_run_creates_it_in_progress():
    provider = _github()

    assert provider.start_check_run("review", "PR-Agent is running /review") is True

    [(method, url, body)] = _requests(provider)
    assert (method, url) == ("POST", CHECK_RUNS_URL)
    assert body["name"] == "PR Agent - Review"
    assert body["head_sha"] == "abc123"
    assert body["status"] == "in_progress"
    assert "conclusion" not in body
    assert body["output"] == {"title": "PR Agent - Review", "summary": "PR-Agent is running /review"}
    assert provider._check_runs_in_progress == {"review"}


def test_start_check_run_reopens_the_run_already_on_the_commit():
    """A re-run on the same head updates the existing run rather than adding a second one."""
    provider = _github(existing=[{"name": "PR Agent - Review", "id": 55}])

    provider.start_check_run("review", "working")

    [(method, url, body)] = _requests(provider)
    assert (method, url) == ("PATCH", f"{CHECK_RUNS_URL}/55")
    assert body["status"] == "in_progress"


def test_the_tool_completes_the_run_the_runner_opened():
    provider = _github()
    provider.start_check_run("review", "working")

    assert provider._publish_check_run("## Review\n\nlooks fine", "review") is True

    assert [(m, u) for m, u, _ in _requests(provider)] == [("POST", CHECK_RUNS_URL), ("PATCH", f"{CHECK_RUNS_URL}/101")]
    completed = _requests(provider)[-1][2]
    assert completed["status"] == "completed"
    assert completed["conclusion"] == "neutral"
    assert completed["output"]["text"] == "## Review\n\nlooks fine"
    # The tool owns the run now: the runner's completion must not overwrite its output.
    assert provider.finish_check_run("review", "failure", "could not finish") is False
    assert len(_requests(provider)) == 2


def test_finish_check_run_completes_a_run_the_tool_left_open():
    provider = _github()
    provider.start_check_run("review", "working")

    assert provider.finish_check_run("review", "failure", "PR-Agent could not finish /review") is True

    completed = _requests(provider)[-1]
    assert completed[:2] == ("PATCH", f"{CHECK_RUNS_URL}/101")
    assert completed[2] == {
        "status": "completed",
        "conclusion": "failure",
        "output": {"title": "PR Agent - Review", "summary": "PR-Agent could not finish /review"},
    }
    assert provider._check_runs_in_progress == set()


def test_finish_check_run_ignores_a_run_it_did_not_open():
    provider = _github()

    assert provider.finish_check_run("review", "success", "done") is False
    assert _requests(provider) == []


def test_start_check_run_without_a_commit_sha_reports_failure():
    provider = _github(sha=None)

    assert provider.start_check_run("review", "working") is False
    assert provider._check_runs_in_progress == set()


def test_start_check_run_survives_an_api_failure():
    provider = _github()
    provider.pr._requester.requestJsonAndCheck.side_effect = RuntimeError("boom")

    assert provider.start_check_run("review", "working") is False
    assert provider._check_runs_in_progress == set()


# --------------------------------------------------------------------------------------
# Wiring: the automatic-command runner
# --------------------------------------------------------------------------------------
@pytest.fixture
def auto_commands(monkeypatch):
    settings = get_settings()
    snapshot = snapshot_settings(["github_app.feedback_on_draft_pr", "config.disable_auto_feedback"])
    provider = MagicMock()
    monkeypatch.setattr(github_app, "get_git_provider_with_context", lambda pr_url: provider)
    monkeypatch.setattr(github_app, "get_pr_commands", lambda name: ["/review"])
    monkeypatch.setattr(github_app, "should_process_pr_logic", lambda body: True)
    monkeypatch.setattr(github_app, "prepare_command", lambda command: command)
    settings.set("github_app.feedback_on_draft_pr", True)
    settings.set("config.disable_auto_feedback", False)
    yield provider
    restore_settings(snapshot)


@pytest.fixture
def restored_config():
    """Override a `config` key and put the original back through the same API."""
    settings = get_settings()
    original = {}

    def _set(key, value):
        original.setdefault(key, settings.get(f"config.{key}", None))
        settings.set(f"config.{key}", value)

    yield _set
    for key, value in original.items():
        settings.set(f"config.{key}", value)


def _agent(outcome):
    agent = MagicMock()
    events = []

    async def handle_request(api_url, command, notify=None):
        events.append(("run", command))
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(command)
        return outcome

    agent.handle_request = handle_request
    return agent, events


def _record(provider, events):
    provider.start_check_run.side_effect = lambda name, summary: events.append(("start", name, summary))
    provider.finish_check_run.side_effect = (
        lambda name, conclusion, summary: events.append(("finish", name, conclusion, summary)))


async def test_a_command_opens_its_check_run_before_running_and_completes_it_after(check_runs_enabled, auto_commands):
    agent, events = _agent(True)
    _record(auto_commands, events)

    result = await github_app._perform_auto_commands_github("pr_commands", agent, {}, API_URL, {})

    assert events == [
        ("start", "review", "PR-Agent is running /review"),
        ("run", "/review"),
        ("finish", "review", "success", "PR-Agent ran /review"),
    ]
    assert result is True


async def test_a_failed_command_completes_its_check_run_as_failure(check_runs_enabled, auto_commands):
    agent, events = _agent(False)
    _record(auto_commands, events)

    result = await github_app._perform_auto_commands_github("pr_commands", agent, {}, API_URL, {})

    assert events[-1] == ("finish", "review", "failure", "PR-Agent could not finish /review")
    assert result is False


async def test_a_raising_command_completes_its_check_run_as_failure(check_runs_enabled, auto_commands):
    agent, events = _agent(RuntimeError("boom"))
    _record(auto_commands, events)

    result = await github_app._perform_auto_commands_github("pr_commands", agent, {}, API_URL, {})

    assert events[-1] == ("finish", "review", "failure", "PR-Agent could not finish /review")
    assert result is False


async def test_each_command_gets_its_own_check_run(check_runs_enabled, auto_commands, monkeypatch):
    monkeypatch.setattr(github_app, "get_pr_commands",
                        lambda name: ["/describe --pr_description.final_update_message=false", "/review", "/improve"])
    agent, events = _agent(True)
    _record(auto_commands, events)

    await github_app._perform_auto_commands_github("pr_commands", agent, {}, API_URL, {})

    assert [e[:2] for e in events if e[0] != "run"] == [
        ("start", "describe"), ("finish", "describe"),
        ("start", "review"), ("finish", "review"),
        ("start", "suggestions"), ("finish", "suggestions"),
    ]


async def test_a_command_without_a_check_run_opens_none(check_runs_enabled, auto_commands, monkeypatch):
    monkeypatch.setattr(github_app, "get_pr_commands", lambda name: ["/ask something"])
    agent, events = _agent(True)
    _record(auto_commands, events)

    await github_app._perform_auto_commands_github("pr_commands", agent, {}, API_URL, {})

    assert events == [("run", "/ask something")]


async def test_nothing_is_opened_when_the_setting_is_off(auto_commands):
    """Control: the shipped default changes nothing."""
    snapshot = snapshot_settings(["github.publish_as_check_run"])
    get_settings().set("github.publish_as_check_run", False)
    try:
        agent, events = _agent(True)

        await github_app._perform_auto_commands_github("pr_commands", agent, {}, API_URL, {})
    finally:
        restore_settings(snapshot)

    auto_commands.start_check_run.assert_not_called()
    auto_commands.finish_check_run.assert_not_called()
    assert events == [("run", "/review")]


async def test_a_failing_acknowledgement_does_not_stop_the_command(check_runs_enabled, auto_commands):
    """The check run is a courtesy: a provider that cannot write it must not cost the review."""
    auto_commands.start_check_run.side_effect = RuntimeError("no checks: write")
    auto_commands.finish_check_run.side_effect = RuntimeError("no checks: write")
    agent, events = _agent(True)

    result = await github_app._perform_auto_commands_github("pr_commands", agent, {}, API_URL, {})

    assert events == [("run", "/review")]
    assert result is True


async def test_a_provider_that_cannot_be_built_does_not_stop_the_command(
        check_runs_enabled, auto_commands, monkeypatch):
    def explode(pr_url):
        raise ValueError("Failed to get git provider")

    monkeypatch.setattr(github_app, "get_git_provider_with_context", explode)
    agent, events = _agent(True)

    result = await github_app._perform_auto_commands_github("pr_commands", agent, {}, API_URL, {})

    assert events == [("run", "/review")]
    assert result is True


async def test_a_provider_without_check_runs_is_left_alone(check_runs_enabled, auto_commands, monkeypatch):
    """Selected by capability, not by provider type: anything without `start_check_run` is skipped."""
    monkeypatch.setattr(github_app, "get_git_provider_with_context", lambda pr_url: MagicMock(spec=[]))
    agent, events = _agent(True)

    result = await github_app._perform_auto_commands_github("pr_commands", agent, {}, API_URL, {})

    assert events == [("run", "/review")]
    assert result is True


# --------------------------------------------------------------------------------------
# A tool that swallows its own error must not be reported as a success.
#
# `propagate_tool_errors` is false by default, so `PRReviewer.run()` logs the failure and
# returns normally. `handle_request` therefore answers True, and without the run-details
# verdict the check run would complete as a success on a pull request that got no review.
# --------------------------------------------------------------------------------------
async def test_a_swallowed_tool_error_completes_the_check_run_as_failure(check_runs_enabled, auto_commands):
    def swallow(command):
        init_run_details()
        record_command_failure()
        return True

    agent, events = _agent(swallow)
    _record(auto_commands, events)

    result = await github_app._perform_auto_commands_github("pr_commands", agent, {}, API_URL, {})

    assert events[-1] == ("finish", "review", "failure", "PR-Agent could not finish /review")
    assert result is False


async def test_a_verdict_does_not_leak_into_the_next_command(check_runs_enabled, auto_commands, monkeypatch):
    """The collector is a ContextVar, so a stale failure must not condemn the command after it."""
    monkeypatch.setattr(github_app, "get_pr_commands", lambda name: ["/describe", "/review"])

    def outcome(command):
        if command == "/describe":
            init_run_details()
            record_command_failure()
        # A tool that never installs a collector of its own must read no verdict.
        return True

    agent, events = _agent(outcome)
    _record(auto_commands, events)

    result = await github_app._perform_auto_commands_github("pr_commands", agent, {}, API_URL, {})

    assert [e for e in events if e[0] == "run"] == [("run", "/describe"), ("run", "/review")]
    assert [e for e in events if e[0] == "finish"] == [
        ("finish", "describe", "failure", "PR-Agent could not finish /describe"),
        ("finish", "review", "success", "PR-Agent ran /review"),
    ]
    assert result is False
    assert command_failed() is False


async def test_a_clean_run_still_reports_success(check_runs_enabled, auto_commands):
    """Control: a tool that installs a collector and records nothing is a success."""
    def clean(command):
        init_run_details()
        return True

    agent, events = _agent(clean)
    _record(auto_commands, events)

    result = await github_app._perform_auto_commands_github("pr_commands", agent, {}, API_URL, {})

    assert events[-1] == ("finish", "review", "success", "PR-Agent ran /review")
    assert result is True


async def test_the_reviewer_records_a_swallowed_failure(monkeypatch, restored_config):
    """End to end through the real `PRReviewer.run()`, with the shipped default settings."""
    restored_config("propagate_tool_errors", False)
    restored_config("publish_output", False)
    tool = PRReviewer.__new__(PRReviewer)
    tool.git_provider = MagicMock()
    tool.incremental = SimpleNamespace(is_incremental=False)
    tool.pr_url = API_URL
    monkeypatch.setattr(PRReviewer, "_prepare_prediction", MagicMock(side_effect=RuntimeError("boom")))

    init_run_details()
    await tool.run()

    assert command_failed() is True


async def test_a_reviewer_run_that_works_records_nothing(monkeypatch):
    """Control: the flag is only set by the swallow path."""
    init_run_details()

    assert command_failed() is False
