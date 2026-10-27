import copy
from types import SimpleNamespace

import pytest
from starlette_context import context, request_cycle_context

from pr_agent.algo import language_handler
from pr_agent.algo.setting_utils import normalize_setting_list
from pr_agent.config_loader import global_settings
from pr_agent.servers import bitbucket_app, gitlab_webhook


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, []),
        ("", []),
        ("/review", ["/review"]),
        (["/review", "/describe"], ["/review", "/describe"]),
        (("/review",), ["/review"]),
        ("opened,reopened", ["opened,reopened"]),
    ],
)
def test_normalize_setting_list(value, expected):
    assert normalize_setting_list(value) == expected


def test_normalize_setting_list_can_split_comma_separated_actions():
    assert normalize_setting_list("opened, reopened", split_commas=True) == ["opened", "reopened"]


def test_plain_string_bad_extension_keeps_other_files(monkeypatch):
    settings = SimpleNamespace(
        bad_extensions=SimpleNamespace(default="csv", extra=[]),
        config=SimpleNamespace(use_extra_bad_extensions=False),
    )
    monkeypatch.setattr(language_handler, "get_settings", lambda: settings)

    assert language_handler.is_valid_file("main.c")
    assert not language_handler.is_valid_file("data.csv")


async def test_plain_string_push_command_dispatches_once(monkeypatch):
    class Agent:
        def __init__(self):
            self.commands = []

        async def handle_request(self, _url, command, notify=None):
            self.commands.append(command)

    monkeypatch.setattr(gitlab_webhook, "prepare_command", lambda command: command)
    monkeypatch.setattr(gitlab_webhook, "should_process_pr_logic", lambda _data: True)
    agent = Agent()
    with request_cycle_context({}):
        context["settings"] = copy.deepcopy(global_settings)
        context["settings"].set("GITLAB.PUSH_COMMANDS", "/review")
        await gitlab_webhook._perform_commands_gitlab(
            "push_commands", agent, "https://example.test/pr/1", {}, {"object_attributes": {"title": "MR"}},
        )

    assert agent.commands == ["/review"]


async def test_missing_bitbucket_push_commands_dispatch_nothing(monkeypatch):
    dispatched = []

    async def run_commands(commands, *_args):
        dispatched.append(list(commands))

    async def valid_push(_data):
        return True

    monkeypatch.setattr(bitbucket_app, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(bitbucket_app, "should_process_pr_logic", lambda _data: True)
    monkeypatch.setattr(bitbucket_app, "_validate_time_from_last_commit_to_pr_update", valid_push)
    monkeypatch.setattr(bitbucket_app, "_run_commands_bitbucket", run_commands)
    with request_cycle_context({}):
        context["settings"] = copy.deepcopy(global_settings)
        context["settings"].set("BITBUCKET_APP.HANDLE_PUSH_TRIGGER", True)
        await bitbucket_app._perform_commands_bitbucket("push_commands", object(), "https://example.test/pr/1", {}, {})

    assert dispatched == []
