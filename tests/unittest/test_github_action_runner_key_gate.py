"""The runner's provider-key gate: stop before any tool runs (#4324)."""

import json

import pytest

from pr_agent.servers import github_action_runner


@pytest.fixture
def action_env_factory(monkeypatch, tmp_path):
    def _factory(event, event_name):
        event_path = tmp_path / f"{event_name}.json"
        event_path.write_text(json.dumps(event), encoding="utf-8")
        monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
        monkeypatch.setenv("GITHUB_TOKEN", "token")
        monkeypatch.delenv("OPENAI_KEY", raising=False)
        monkeypatch.delenv("OPENAI.KEY", raising=False)

    return _factory


@pytest.fixture
def action_env(action_env_factory):
    action_env_factory({
        "action": "opened",
        "pull_request": {
            "html_url": "https://github.com/owner/repo/pull/1",
            "url": "https://api.github.com/repos/owner/repo/pulls/1",
        },
    }, "pull_request")


async def test_missing_key_stops_before_tools_naming_the_env_variable(
    monkeypatch, action_env
):
    monkeypatch.setattr(
        github_action_runner, "missing_provider_key_env_vars",
        lambda: ("openai", ("OPENAI_API_KEY",)),
    )

    with pytest.raises(SystemExit, match="Stopping before any tool runs.*OPENAI_API_KEY"):
        await github_action_runner.run_action()


async def test_repository_settings_are_applied_before_key_gate(monkeypatch, action_env):
    order = []
    monkeypatch.setattr(
        github_action_runner, "apply_repo_settings",
        lambda pr_url: order.append(("repo_settings", pr_url)),
    )
    monkeypatch.setattr(
        github_action_runner, "missing_provider_key_env_vars",
        lambda: order.append("gate") or ("openai", ("OPENAI_API_KEY",)),
    )
    monkeypatch.setattr(
        github_action_runner, "get_setting_or_env", lambda key, default=None: default,
    )

    # A repo-level .pr_agent.toml can select a different provider, so its settings must
    # be applied before the gate judges the active provider's key.
    with pytest.raises(SystemExit):
        await github_action_runner.run_action()
    assert order == [("repo_settings", "https://github.com/owner/repo/pull/1"), "gate"]


@pytest.mark.parametrize(
    "event_name, event, expected_url",
    [
        (
            "issue_comment",
            {
                "action": "created",
                "comment": {"body": "/review"},
                "issue": {"number": 1, "pull_request": {
                    "html_url": "https://github.com/owner/repo/pull/1",
                    "url": "https://api.github.com/repos/owner/repo/pulls/1",
                }},
            },
            "https://github.com/owner/repo/pull/1",
        ),
        (
            "workflow_run",
            {
                "action": "workflow_run",
                "workflow_run": {
                    "event": "pull_request",
                    "conclusion": "success",
                    "pull_requests": [{"url": "https://api.github.com/repos/owner/repo/pulls/1"}],
                },
            },
            "https://api.github.com/repos/owner/repo/pulls/1",
        ),
    ],
)
async def test_repository_settings_are_applied_before_key_gate_on_comment_and_workflow_run_events(
    monkeypatch, action_env_factory, event_name, event, expected_url
):
    order = []
    monkeypatch.setattr(
        github_action_runner, "apply_repo_settings",
        lambda pr_url: order.append(("repo_settings", pr_url)),
    )
    monkeypatch.setattr(
        github_action_runner, "missing_provider_key_env_vars",
        lambda: order.append("gate") or ("openai", ("OPENAI_API_KEY",)),
    )
    monkeypatch.setattr(
        github_action_runner, "get_setting_or_env", lambda key, default=None: default,
    )
    action_env_factory(event, event_name)

    # The gate must judge the provider the repo-level settings select, whatever event
    # shape carried the PR.
    with pytest.raises(SystemExit):
        await github_action_runner.run_action()
    assert order == [("repo_settings", expected_url), "gate"]


@pytest.mark.parametrize(
    "event_name, event",
    [
        (
            "issue_comment",
            {
                "action": "created",
                "issue": {"number": 1},
                "comment": {"body": "not a command"},
            },
        ),
        (
            "workflow_run",
            {
                "action": "workflow_run",
                "workflow_run": {"event": "push", "conclusion": "success"},
            },
        ),
    ],
)
async def test_events_without_a_pr_skip_the_gate(
    monkeypatch, action_env_factory, event_name, event
):
    calls = []
    monkeypatch.setattr(
        github_action_runner, "apply_repo_settings",
        lambda pr_url: calls.append(("repo_settings", pr_url)),
    )
    monkeypatch.setattr(
        github_action_runner, "missing_provider_key_env_vars",
        lambda: calls.append("gate") or ("openai", ("OPENAI_API_KEY",)),
    )
    monkeypatch.setattr(
        github_action_runner, "get_setting_or_env", lambda key, default=None: default,
    )
    action_env_factory(event, event_name)

    # No PR, no repo settings: the run decides, as on main.
    await github_action_runner.run_action()
    assert calls == []


@pytest.mark.parametrize(
    "event_name, action, sender_type, body",
    [
        ("issue_comment", "created", "User", "Thanks, this looks good."),
        ("pull_request_review_comment", "created", "User", "Thanks!"),
        ("issue_comment", "edited", "User", "  "),
        ("issue_comment", "created", "User", ""),
        ("pull_request_review_comment", "created", "User", None),
        ("issue_comment", "created", "Bot", "/review"),
        ("pull_request_review_comment", "deleted", "User", "/review"),
    ],
)
async def test_ignored_pr_comments_skip_repository_settings_and_key_gate(
    monkeypatch, action_env_factory, event_name, action, sender_type, body
):
    calls = []

    async def handle_request(url, command, notify=None):
        calls.append("dispatch")

    monkeypatch.setattr(github_action_runner, "_handle_request", handle_request)
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda url: calls.append("repo_settings"))
    monkeypatch.setattr(
        github_action_runner, "missing_provider_key_env_vars",
        lambda: calls.append("gate") or ("openai", ("OPENAI_API_KEY",)),
    )
    pr = {"html_url": "https://github.com/owner/repo/pull/1", "url": "https://api.github.com/repos/owner/repo/pulls/1"}
    event = {"action": action, "sender": {"type": sender_type}, "comment": {"body": body}}
    if event_name == "issue_comment":
        event["issue"] = {"pull_request": pr}
    else:
        event["pull_request"] = pr
    action_env_factory(event, event_name)

    await github_action_runner.run_action()

    assert calls == []


@pytest.mark.parametrize("body", ["/ask Why?", "/unknown"])
async def test_inline_command_comments_keep_setup_before_line_conversion(monkeypatch, action_env_factory, body):
    order = []
    converted = ["/ask_line", "Why?"]
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda url: order.append("repo_settings"))
    monkeypatch.setattr(github_action_runner, "missing_provider_key_env_vars", lambda: order.append("gate"))
    monkeypatch.setattr(github_action_runner, "_inject_artifact_context", lambda: None)

    def convert_line_comment(event, comment_body):
        assert order == ["repo_settings", "gate"]
        assert comment_body == "/ask Why?"
        order.append("line_conversion")
        return converted

    async def handle_request(url, command, notify=None):
        order.append(("dispatch", command))

    monkeypatch.setattr(github_action_runner, "handle_line_comments", convert_line_comment)
    monkeypatch.setattr(github_action_runner, "_handle_request", handle_request)
    pr = {"html_url": "https://github.com/owner/repo/pull/1", "url": "https://api.github.com/repos/owner/repo/pulls/1"}
    action_env_factory({
        "action": "created",
        "pull_request": pr,
        "comment": {"body": body, "pull_request_url": pr["url"], "id": 123},
        "sender": {"type": "User"},
    }, "pull_request_review_comment")

    await github_action_runner.run_action()

    expected = ["repo_settings", "gate"]
    if body.startswith("/ask"):
        expected += ["line_conversion", ("dispatch", converted)]
    else:
        expected.append(("dispatch", body))
    assert order == expected
