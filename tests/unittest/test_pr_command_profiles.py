import copy
import json
import tomllib
from pathlib import Path

import pytest
from dynaconf.loaders import env_loader
from starlette.background import BackgroundTasks
from starlette_context import context, request_cycle_context

from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers.utils import _apply_repo_settings_file
from pr_agent.identity_providers.identity_provider import Eligibility
from pr_agent.servers import (
    azuredevops_server_webhook,
    bitbucket_app,
    bitbucket_server_webhook,
    gitea_app,
    github_app,
    gitlab_webhook,
)
from pr_agent.servers import (
    utils as server_utils,
)

DEFAULT_PR_COMMANDS = {
    "github_app": [
        "/describe --pr_description.final_update_message=false",
        "/review",
        "/improve",
    ],
    "gitlab": [
        "/describe --pr_description.final_update_message=false",
        "/review",
        "/improve",
    ],
    "gitea": ["/describe", "/review", "/improve"],
    "azure_devops_server": ["/describe", "/review", "/improve"],
    "bitbucket_app": [
        "/describe --pr_description.final_update_message=false",
        "/review",
        "/improve --pr_code_suggestions.commitable_code_suggestions=true",
    ],
    "bitbucket_server": [
        "/describe --pr_description.final_update_message=false",
        "/review",
        "/improve --pr_code_suggestions.commitable_code_suggestions=true",
    ],
}


class _Settings:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key.lower(), default)


class _RecordingAgent:
    def __init__(self):
        self.commands = []

    async def handle_request(self, _url, command, notify=None):
        self.commands.append(command)


class _IdentityProvider:
    def verify_eligibility(self, *_args):
        return Eligibility.ELIGIBLE


class _Request:
    def __init__(self, payload):
        self.headers = {"authorization": "JWT e30.eyJpc3MiOiJjbGllbnQifQ.signature"}
        self._payload = payload

    async def json(self):
        return self._payload

    async def body(self):
        return json.dumps(self._payload).encode()


@pytest.mark.parametrize(("provider", "expected"), DEFAULT_PR_COMMANDS.items())
def test_default_pr_command_profiles_match_existing_provider_behavior(monkeypatch, provider, expected):
    monkeypatch.setattr(server_utils, "get_settings", _Settings)

    assert server_utils.get_pr_commands(provider) == expected


def test_default_pr_commands_are_fresh_lists(monkeypatch):
    monkeypatch.setattr(server_utils, "get_settings", _Settings)

    first = server_utils.get_pr_commands("github_app")
    second = server_utils.get_pr_commands("github_app")
    first.append("/ask")

    assert second == DEFAULT_PR_COMMANDS["github_app"]


def test_missing_dynaconf_key_uses_the_default_profile(monkeypatch):
    settings = copy.deepcopy(global_settings)
    settings.github_app.pop("pr_commands", None)
    monkeypatch.setattr(server_utils, "get_settings", lambda: settings)

    assert server_utils.get_pr_commands("github_app") == DEFAULT_PR_COMMANDS["github_app"]


@pytest.mark.parametrize("override", [[], "", ["/custom"]])
def test_explicit_pr_command_overrides_are_preserved(monkeypatch, override):
    configured = copy.deepcopy(override)
    settings = _Settings({"github_app.pr_commands": configured})
    monkeypatch.setattr(server_utils, "get_settings", lambda: settings)

    assert server_utils.get_pr_commands("github_app") is configured


def test_provider_sections_no_longer_duplicate_pr_command_defaults():
    configuration_path = Path(__file__).parents[2] / "pr_agent/settings/configuration.toml"
    configuration = tomllib.loads(configuration_path.read_text(encoding="utf-8"))

    for provider in DEFAULT_PR_COMMANDS:
        assert "pr_commands" not in configuration[provider]


def test_repo_pr_commands_override_the_default_profile(tmp_path):
    repo_settings = tmp_path / ".pr_agent.toml"
    repo_settings.write_text('[github_app]\npr_commands = ["/repo"]\n')

    with request_cycle_context({}):
        context["settings"] = copy.deepcopy(global_settings)
        _apply_repo_settings_file(str(repo_settings))

        assert server_utils.get_pr_commands("github_app") == ["/repo"]


def test_environment_pr_commands_win_over_repo_settings(tmp_path, monkeypatch):
    repo_settings = tmp_path / ".pr_agent.toml"
    repo_settings.write_text('[github_app]\npr_commands = ["/repo"]\n')
    monkeypatch.setenv("GITHUB_APP__PR_COMMANDS", '["/environment"]')

    with request_cycle_context({}):
        context["settings"] = copy.deepcopy(global_settings)
        env_loader.load(get_settings())
        _apply_repo_settings_file(str(repo_settings))

        assert server_utils.get_pr_commands("github_app") == ["/environment"]


async def _dispatch_default_pr_commands(provider, monkeypatch, agent):
    if provider == "github_app":
        monkeypatch.setattr(github_app, "prepare_command", lambda command: command)
        monkeypatch.setattr(github_app, "should_process_pr_logic", lambda _body: True)
        await github_app._perform_auto_commands_github(
            "pr_commands",
            agent,
            {"action": "opened", "pull_request": {"draft": False}},
            "https://example.test/pr/1",
            {},
        )
    elif provider == "gitlab":
        monkeypatch.setattr(gitlab_webhook, "prepare_command", lambda command: command)
        monkeypatch.setattr(gitlab_webhook, "should_process_pr_logic", lambda _body: True)
        await gitlab_webhook._perform_commands_gitlab(
            "pr_commands",
            agent,
            "https://example.test/pr/1",
            {},
            {"object_attributes": {"title": "Ready MR"}},
        )
    elif provider == "gitea":
        monkeypatch.setattr(gitea_app, "apply_repo_settings", lambda _url: None)
        monkeypatch.setattr(gitea_app, "prepare_command", lambda command: command)
        monkeypatch.setattr(gitea_app, "should_process_pr_logic", lambda _body: True)
        await gitea_app._perform_commands_gitea(
            "pr_commands",
            agent,
            {},
            "https://example.test/pr/1",
        )
    elif provider == "bitbucket_app":
        monkeypatch.setattr(bitbucket_app, "apply_repo_settings", lambda _url: None)
        monkeypatch.setattr(bitbucket_app, "prepare_command", lambda command: command)
        monkeypatch.setattr(bitbucket_app, "should_process_pr_logic", lambda _body: True)
        await bitbucket_app._perform_commands_bitbucket(
            "pr_commands",
            agent,
            "https://example.test/pr/1",
            {},
            {"event": "pullrequest:created"},
        )
    elif provider == "azure_devops_server":
        monkeypatch.setattr(azuredevops_server_webhook, "apply_repo_settings", lambda _url: None)
        monkeypatch.setattr(azuredevops_server_webhook, "prepare_command", lambda command: command)
        await azuredevops_server_webhook._perform_commands_azure(
            "pr_commands",
            agent,
            "https://example.test/pr/1",
            {},
        )
    else:
        monkeypatch.setattr(bitbucket_server_webhook, "apply_repo_settings", lambda _url: None)
        monkeypatch.setattr(bitbucket_server_webhook, "should_process_pr_logic", lambda _body: True)
        recorded = []

        async def record_commands(commands, _url, _log_context):
            recorded.extend(commands)

        monkeypatch.setattr(bitbucket_server_webhook, "_run_commands_sequentially", record_commands)
        payload = {
            "eventKey": "pr:opened",
            "pullRequest": {
                "id": 1,
                "toRef": {"repository": {"slug": "repo", "project": {"key": "project"}}},
            },
        }
        background_tasks = BackgroundTasks()
        response = await bitbucket_server_webhook.handle_webhook(background_tasks, _Request(payload))
        assert response.status_code == 200
        await background_tasks()
        agent.commands.extend(recorded)


@pytest.mark.parametrize("provider", DEFAULT_PR_COMMANDS)
async def test_each_webhook_dispatches_its_default_profile(provider, monkeypatch):
    agent = _RecordingAgent()
    monkeypatch.setattr(server_utils, "get_settings", _Settings)

    with request_cycle_context({}):
        context["settings"] = copy.deepcopy(global_settings)
        await _dispatch_default_pr_commands(provider, monkeypatch, agent)

    assert agent.commands == DEFAULT_PR_COMMANDS[provider]


async def test_bitbucket_app_default_profile_passes_the_early_gate(monkeypatch):
    calls = []
    payload = {
        "event": "pullrequest:created",
        "data": {
            "actor": {"type": "user", "account_id": "account"},
            "pullrequest": {"links": {"html": {"href": "https://example.test/pr/1"}}},
        },
    }
    secret_provider = type(
        "SecretProvider",
        (),
        {"get_secret": lambda self, _key: json.dumps({"shared_secret": "secret"})},
    )()

    async def get_bearer_token(_shared_secret, _client_key):
        return "bearer"

    async def perform_commands(*args):
        calls.append(args)

    monkeypatch.setattr(server_utils, "get_settings", _Settings)
    monkeypatch.setattr(bitbucket_app, "is_bot_user", lambda _data: False)
    monkeypatch.setattr(bitbucket_app, "should_process_pr_logic", lambda _data: True)
    monkeypatch.setattr(bitbucket_app, "get_fork_safe_secret_provider", lambda: secret_provider)
    monkeypatch.setattr(bitbucket_app, "get_bearer_token", get_bearer_token)
    monkeypatch.setattr(bitbucket_app.jwt, "decode", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        bitbucket_app,
        "get_identity_provider",
        _IdentityProvider,
    )
    monkeypatch.setattr(bitbucket_app, "_perform_commands_bitbucket", perform_commands)
    endpoint = next(route.endpoint for route in bitbucket_app.router.routes if route.path == "/webhook")
    background_tasks = BackgroundTasks()

    with request_cycle_context({}):
        context["settings"] = copy.deepcopy(global_settings)
        get_settings().set("BITBUCKET.BASE_URL", "https://example.test/app")
        result = await endpoint(background_tasks, _Request(payload))
        await background_tasks()

    assert result == "OK"
    assert len(calls) == 1
    assert calls[0][0] == "pr_commands"
