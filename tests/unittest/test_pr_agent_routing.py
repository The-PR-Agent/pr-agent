from unittest.mock import Mock

import pytest

import pr_agent.agent.pr_agent as pr_agent_module
from pr_agent.config_loader import get_settings


def _identity_args(args):
    return args


@pytest.fixture(autouse=True)
def reset_response_language():
    settings = get_settings()
    original_response_language = settings.config.response_language
    settings.config.response_language = "en-us"
    try:
        yield
    finally:
        settings.config.response_language = original_response_language


def _patch_request_dependencies(monkeypatch, validate_result=(True, None), update_settings_fn=None):
    if update_settings_fn is None:
        update_settings_fn = _identity_args

    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(pr_agent_module.CliArgs, "validate_user_args", lambda args: validate_result)
    monkeypatch.setattr(pr_agent_module, "update_settings_from_args", update_settings_fn)


@pytest.mark.asyncio
async def test_handle_request_routes_known_command_and_notifies(monkeypatch):
    runs = []
    notify = Mock()

    class FakeTool:
        def __init__(self, pr_url, ai_handler, args):
            self.pr_url = pr_url
            self.ai_handler = ai_handler
            self.args = args

        async def run(self):
            runs.append((self.pr_url, self.ai_handler, self.args))

    _patch_request_dependencies(monkeypatch, update_settings_fn=lambda args: ["--kept"])
    monkeypatch.setitem(pr_agent_module.command2class, "custom", FakeTool)

    handled = await pr_agent_module.PRAgent(ai_handler="fake-ai")._handle_request(
        "https://example/pr/1", "/custom --flag", notify
    )

    assert handled is True
    notify.assert_called_once_with()
    assert runs == [("https://example/pr/1", "fake-ai", ["--kept"])]


@pytest.mark.asyncio
async def test_handle_request_routes_list_request_without_string_parsing(monkeypatch):
    runs = []

    class FakeTool:
        def __init__(self, pr_url, ai_handler, args):
            self.pr_url = pr_url
            self.ai_handler = ai_handler
            self.args = args

        async def run(self):
            runs.append((self.pr_url, self.ai_handler, self.args))

    _patch_request_dependencies(monkeypatch)
    monkeypatch.setitem(pr_agent_module.command2class, "custom", FakeTool)

    handled = await pr_agent_module.PRAgent(ai_handler="fake-ai")._handle_request(
        "https://example/pr/1",
        ["/custom", "don't split", "--flag=value"],
    )

    assert handled is True
    assert runs == [("https://example/pr/1", "fake-ai", ["don't split", "--flag=value"])]


@pytest.mark.asyncio
async def test_handle_request_rejects_forbidden_cli_args(monkeypatch):
    class FakeTool:
        async def run(self):
            raise AssertionError("tool should not run")

    _patch_request_dependencies(monkeypatch, validate_result=(False, "secret"))
    monkeypatch.setitem(pr_agent_module.command2class, "custom", FakeTool)

    handled = await pr_agent_module.PRAgent()._handle_request("https://example/pr/1", "/custom --secret=value")

    assert handled is False


@pytest.mark.asyncio
async def test_handle_request_wrapper_returns_false_on_exception(monkeypatch):
    async def raise_error(self, pr_url, request, notify=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(pr_agent_module.PRAgent, "_handle_request", raise_error)

    handled = await pr_agent_module.PRAgent().handle_request("https://example/pr/1", "/review")

    assert handled is False


@pytest.mark.asyncio
async def test_handle_request_answer_uses_reviewer_answer_mode_and_notifies(monkeypatch):
    calls = []
    notify = Mock()

    class FakeReviewer:
        def __init__(self, pr_url, is_answer=False, is_auto=False, args=None, ai_handler=None):
            calls.append({
                "pr_url": pr_url,
                "is_answer": is_answer,
                "is_auto": is_auto,
                "args": args,
                "ai_handler": ai_handler,
            })

        async def run(self):
            calls[-1]["ran"] = True

    _patch_request_dependencies(monkeypatch)
    monkeypatch.setattr(pr_agent_module, "PRReviewer", FakeReviewer)

    handled = await pr_agent_module.PRAgent(ai_handler="fake-ai")._handle_request(
        "https://example/pr/1", "/answer yes", notify
    )

    assert handled is True
    notify.assert_called_once_with()
    assert calls == [{
        "pr_url": "https://example/pr/1",
        "is_answer": True,
        "is_auto": False,
        "args": ["yes"],
        "ai_handler": "fake-ai",
        "ran": True,
    }]


@pytest.mark.asyncio
async def test_handle_request_answer_preserves_quoted_question_as_single_arg(monkeypatch):
    calls = []

    class FakeReviewer:
        def __init__(self, pr_url, is_answer=False, is_auto=False, args=None, ai_handler=None):
            calls.append(args)

        async def run(self):
            pass

    _patch_request_dependencies(monkeypatch)
    monkeypatch.setattr(pr_agent_module, "PRReviewer", FakeReviewer)

    handled = await pr_agent_module.PRAgent()._handle_request(
        "https://example/pr/1", "/answer \"because prod is broken\""
    )

    assert handled is True
    assert calls == [["because prod is broken"]]


@pytest.mark.asyncio
async def test_handle_request_auto_review_uses_reviewer_auto_mode(monkeypatch):
    calls = []

    class FakeReviewer:
        def __init__(self, pr_url, is_answer=False, is_auto=False, args=None, ai_handler=None):
            calls.append((pr_url, is_answer, is_auto, args, ai_handler))

        async def run(self):
            pass

    _patch_request_dependencies(monkeypatch)
    monkeypatch.setattr(pr_agent_module, "PRReviewer", FakeReviewer)

    handled = await pr_agent_module.PRAgent(ai_handler="fake-ai")._handle_request(
        "https://example/pr/1", "/auto_review"
    )

    assert handled is True
    assert calls == [("https://example/pr/1", False, True, [], "fake-ai")]


@pytest.mark.asyncio
async def test_handle_request_returns_false_for_unknown_command(monkeypatch):
    _patch_request_dependencies(monkeypatch)

    handled = await pr_agent_module.PRAgent()._handle_request("https://example/pr/1", "/unknown")

    assert handled is False
