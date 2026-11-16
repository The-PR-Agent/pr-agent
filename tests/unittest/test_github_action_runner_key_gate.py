"""Verify the runner's model-key gate while allowing bare static commands (#4324)."""

import copy
import json
from unittest.mock import Mock

import pytest
from starlette_context import request_cycle_context

from pr_agent import doctor, git_providers
from pr_agent.agent import pr_agent as pr_agent_module
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.servers import github_action_runner
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


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


@pytest.fixture
def pr_comment_env(action_env_factory):
    def _factory(body, event_name):
        pr = {
            "html_url": "https://github.com/owner/repo/pull/1",
            "url": "https://api.github.com/repos/owner/repo/pulls/1",
        }
        event = {
            "action": "created", "sender": {"type": "User"},
            "comment": {"body": body, "id": 123, "pull_request_url": pr["url"]},
        }
        if event_name == "issue_comment":
            event["issue"] = {"pull_request": pr}
        else:
            event["pull_request"] = pr
        action_env_factory(event, event_name)

    return _factory


@pytest.mark.parametrize("event_name", ["issue_comment", "pull_request_review_comment"])
@pytest.mark.parametrize("body", ["/help", "/config", "/settings", " \t/HELP \n", " /CoNfIg \t", "\n/SETTINGS "])
async def test_static_pr_comments_render_without_model_key(monkeypatch, pr_comment_env, event_name, body):
    class StaticProvider(GithubProvider):
        def __init__(self, pr_url=None):
            super().__init__(None)
            self.pr_url = pr_url

        publish_comment = Mock()
        remove_initial_comment = Mock()
        add_eyes_reaction = Mock()

        def get_request_policy_metadata(self, required_fields):
            return {"title": "Test PR"}

    gate = Mock(return_value=("openai", ("OPENAI_API_KEY",)))
    ai_init = Mock(side_effect=AssertionError("Static commands must not initialize an AI handler"))
    monkeypatch.setattr(github_action_runner, "missing_provider_key_env_vars", gate)
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda url: None)
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda url: None)
    monkeypatch.setattr(pr_agent_module.LiteLLMAIHandler, "__init__", ai_init)
    monkeypatch.setitem(git_providers._GIT_PROVIDERS, "github", StaticProvider)
    monkeypatch.setitem(get_settings().config, "git_provider", "github")
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    pr_comment_env(body, event_name)

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        await github_action_runner.run_action()

    gate.assert_not_called()
    ai_init.assert_not_called()
    StaticProvider.publish_comment.assert_called_once()
    expected = "PR Agent Walkthrough" if body.strip().lower() == "/help" else "config.model ="
    assert expected in StaticProvider.publish_comment.call_args.args[0]


@pytest.mark.parametrize("event_name", ["issue_comment", "pull_request_review_comment"])
@pytest.mark.parametrize("body", ["/help How do I configure reviews?", "/review", "/configuration", "/config typo"])
async def test_other_pr_commands_still_require_model_key(monkeypatch, pr_comment_env, event_name, body):
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda url: None)
    monkeypatch.setattr(
        github_action_runner, "missing_provider_key_env_vars",
        lambda: ("openai", ("OPENAI_API_KEY",)),
    )
    pr_comment_env(body, event_name)

    with pytest.raises(SystemExit, match="Stopping before any command runs.*OPENAI_API_KEY"):
        await github_action_runner.run_action()


@pytest.mark.parametrize("event_name", ["issue_comment", "pull_request_review_comment"])
async def test_static_pr_comments_still_require_github_token(monkeypatch, pr_comment_env, event_name):
    pr_comment_env("/help", event_name)
    monkeypatch.delenv("GITHUB_TOKEN")

    with pytest.raises(SystemExit, match="GITHUB_TOKEN not set"):
        await github_action_runner.run_action()


async def test_missing_key_stops_before_commands_naming_the_env_variable(
    monkeypatch, action_env
):
    monkeypatch.setattr(
        github_action_runner, "missing_provider_key_env_vars",
        lambda: ("openai", ("OPENAI_API_KEY",)),
    )

    with pytest.raises(SystemExit, match="Stopping before any command runs.*OPENAI_API_KEY"):
        await github_action_runner.run_action()


async def test_placeholder_without_known_env_names_has_actionable_guidance(monkeypatch, action_env):
    monkeypatch.setattr(
        github_action_runner, "missing_provider_key_env_vars",
        lambda: ("unlisted_provider", ()),
    )

    with pytest.raises(SystemExit) as error:
        await github_action_runner.run_action()

    message = str(error.value)
    assert "Replace the placeholder key in provider settings or workflow env." in message
    assert "Set  in this workflow" not in message


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
    assert order == [("repo_settings", "https://api.github.com/repos/owner/repo/pulls/1"), "gate"]


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
            "https://api.github.com/repos/owner/repo/pulls/1",
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


@pytest.mark.parametrize("provider", ["watsonx", "watsonx_text"])
@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("LITELLM.EXTRA_HEADERS.Authorization", ""),
        ("LITELLM.EXTRA_HEADERS.Authorization", "Bearer"),
        ("LITELLM.EXTRA_HEADERS.Authorization", "Bearer dummy_key"),
        ("LITELLM.EXTRA_HEADERS.Authorization", "ZenApiKey dummy_key"),
        ("WATSONX_TOKEN", "dummy_key"),
        ("WATSONX_ZENAPIKEY", "dummy_key"),
    ],
)
async def test_watsonx_gate_names_the_override_that_must_be_corrected(
    monkeypatch, action_env, capsys, provider, source, value,
):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    # Watsonx's authentication checks must not trigger unrelated AWS discovery.
    monkeypatch.setattr(LiteLLMAIHandler, "_initialize_aws_request_credentials", lambda self, settings: None)
    settings = get_settings()
    snapshot = snapshot_settings((
        "config.model", "litellm.custom_llm_provider", "litellm.extra_headers",
        "openai.api_type", "openai.org", "watsonx", "watsonx_text",
        "github.user_token", "github.deployment_type", "github_action_config.enable_output",
    ))
    secret = "synthetic-native-key-not-for-diagnostics"
    dispatches = []

    async def run_command(*args):
        dispatches.append(args)

    try:
        for key in ("watsonx", "watsonx_text"):
            settings.set(key, {})
        settings.set("config.model", f"{provider}/ibm/granite-3-8b-instruct")
        for key in ("litellm.custom_llm_provider", "litellm.extra_headers", "openai.api_type"):
            settings.set(key, None)
        for name in ("WATSONX_APIKEY", "WATSONX_API_KEY", "WX_API_KEY", "WATSONX_TOKEN", "WATSONX_ZENAPIKEY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("WATSONX_APIKEY", secret)
        monkeypatch.setenv("WATSONX_URL", "https://watsonx.example")
        if source == "LITELLM.EXTRA_HEADERS.Authorization":
            headers = {"authorization": value, "X-Private": secret}
            if value == "Bearer dummy_key":
                headers = {"Authorization": "Bearer valid-earlier-entry", **headers}
            settings.set("litellm.extra_headers", json.dumps(headers))
        else:
            monkeypatch.setenv(source, value)
        monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda url: None)
        monkeypatch.setattr(github_action_runner, "enforce_request_policy", lambda *args, **kwargs: True)
        monkeypatch.setattr(github_action_runner, "_run_auto_command", run_command)

        # Use the real handler and shared gate; a valid native key cannot fix an overriding credential.
        assert doctor.missing_provider_key_env_vars() == (provider, (source,))
        with pytest.raises(SystemExit) as error:
            await github_action_runner.run_action()
        message = str(error.value)
        if source == "LITELLM.EXTRA_HEADERS.Authorization":
            assert "last case-insensitive Authorization entry in LITELLM.EXTRA_HEADERS" in message
        else:
            assert f"Correct or remove {source}" in message
        assert "Set WATSONX_API" not in message
        assert secret not in message
        assert dispatches == []
        output = capsys.readouterr()
        assert secret not in output.out + output.err

        # Removing the indicated source allows the already-present native key to be used.
        if source == "LITELLM.EXTRA_HEADERS.Authorization":
            settings.set("litellm.extra_headers", None)
        else:
            monkeypatch.delenv(source)
        assert doctor.missing_provider_key_env_vars() is None
    finally:
        restore_settings(snapshot)


@pytest.mark.parametrize("locale_encoding", ["cp1252", "ascii"])
@pytest.mark.parametrize("text", ["café", "שלום", "你好 👋"])
async def test_event_payload_is_read_as_utf8_on_non_utf8_locales(
    monkeypatch, tmp_path, locale_encoding, text
):
    # GitHub writes the event file as raw UTF-8 (not \uXXXX escapes). Reading it with the
    # runner's locale encoding turned a non-ASCII PR title into mojibake on cp1252 and raised
    # UnicodeDecodeError on an ASCII locale. Emulate the locale through the module's file
    # opener, as test_github_action_output.py does for GITHUB_OUTPUT, and stop at the first
    # helper that receives the parsed payload.
    event = {
        "action": "opened",
        "pull_request": {"title": text, "url": "https://api.github.com/repos/owner/repo/pulls/1"},
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    real_open = open

    def locale_open(path, mode="r", **kwargs):
        kwargs.setdefault("encoding", locale_encoding)
        return real_open(path, mode, **kwargs)

    monkeypatch.setattr(github_action_runner, "open", locale_open, raising=False)

    class _Stop(Exception):
        pass

    received = {}

    def capture(event_payload, event_name):
        received["title"] = event_payload["pull_request"]["title"]
        raise _Stop

    monkeypatch.setattr(github_action_runner, "_settings_pr_url", capture)

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}), pytest.raises(_Stop):
        await github_action_runner.run_action()

    assert received["title"] == text
