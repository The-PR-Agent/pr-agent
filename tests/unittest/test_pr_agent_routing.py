import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette_context import request_cycle_context

import pr_agent.agent.pr_agent as pr_agent_module
from pr_agent.algo import artifacts
from pr_agent.algo.utils import update_settings_from_args
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import utils as provider_utils


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


def _incomplete_files_provider(comments=()):
    provider = Mock()
    provider.get_issue_comments_newest_first.return_value = list(comments)
    provider._get_comment_body.side_effect = lambda comment: comment.get("body", "")
    provider.is_comment_authored_by_pr_agent.return_value = False
    provider.supports_html_comment_markers.return_value = True
    return provider


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
async def test_prepared_override_wins_after_repo_settings_and_next_command_reloads_defaults(monkeypatch):
    observed = []
    provider = SimpleNamespace(
        get_repo_settings=lambda: b'[pr_reviewer]\nextra_instructions = "repository default"\n'
    )

    class FakeReview:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self):
            observed.append(get_settings().get("PR_REVIEWER.EXTRA_INSTRUCTIONS"))

    monkeypatch.setattr(provider_utils, "get_git_provider_with_context", lambda _url: provider)
    monkeypatch.setattr(pr_agent_module, "reapply_artifact_context", lambda: None)
    monkeypatch.setattr(pr_agent_module, "flush_telemetry", lambda: None)
    monkeypatch.setitem(pr_agent_module.command2class, "review", FakeReview)

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        get_settings().set("CONFIG.USE_REPO_SETTINGS_FILE", True)
        get_settings().set("CONFIG.ENABLE_PER_DIRECTORY_SETTINGS", False)
        get_settings().set("CONFIG.EXTRA_CONFIG_URL", None)
        get_settings().set("PR_REVIEWER.EXTRA_INSTRUCTIONS", "initial")
        worker = pr_agent_module.PRAgent(ai_handler="fake-ai")
        for command in ['/review --pr_reviewer.extra_instructions="command value"', "/review"]:
            assert await worker.handle_request(
                "https://example.com/org/repo/pull/1", pr_agent_module.prepare_command(command)
            ) is True

    assert observed == ["command value", "repository default"]


def test_prepare_command_preserves_spaces_in_quoted_config_values():
    settings = get_settings()
    setting_key = "PR_REVIEWER.EXTRA_INSTRUCTIONS"
    original = settings.get(setting_key)

    try:
        command = pr_agent_module.prepare_command(
            '/review --pr_reviewer.extra_instructions="Focus on authentication and authorization"'
        )

        assert command[0] == "/review"
        assert settings.get(setting_key) == original
        assert update_settings_from_args(command[1:]) == []
        assert settings.get(setting_key) == "Focus on authentication and authorization"
    finally:
        settings.set(setting_key, original)


def test_prepare_command_preserves_quoted_non_setting_arguments():
    command = pr_agent_module.prepare_command('/ask "why is this change risky?"')

    assert command == ["/ask", "why is this change risky?"]


@pytest.mark.parametrize(
    ("quoted_value", "expected_value"),
    [
        ('"Focus on # authentication"', "Focus on # authentication"),
        ('"true"', "true"),
        ('"null"', "null"),
        ("'Focus on # authentication'", "Focus on # authentication"),
    ],
)
def test_prepare_command_preserves_quoted_yaml_sensitive_values(quoted_value, expected_value):
    settings = get_settings()
    setting_key = "PR_REVIEWER.EXTRA_INSTRUCTIONS"
    original = settings.get(setting_key)

    try:
        command = pr_agent_module.prepare_command(
            f"/review --pr_reviewer.extra_instructions={quoted_value}"
        )

        assert command[0] == "/review"
        assert settings.get(setting_key) == original
        assert update_settings_from_args(command[1:]) == []
        assert settings.get(setting_key) == expected_value
    finally:
        settings.set(setting_key, original)


def test_prepare_command_accepts_apostrophes_in_unquoted_arguments_and_values():
    settings = get_settings()
    setting_key = "PR_REVIEWER.EXTRA_INSTRUCTIONS"
    original = settings.get(setting_key)

    try:
        command = pr_agent_module.prepare_command(
            "/review --pr_reviewer.extra_instructions=O'Reilly"
        )

        assert command[0] == "/review"
        assert settings.get(setting_key) == original
        assert update_settings_from_args(command[1:]) == []
        assert settings.get(setting_key) == "O'Reilly"
        assert pr_agent_module.prepare_command("/ask What's wrong?") == [
            "/ask",
            "What's",
            "wrong?",
        ]
    finally:
        settings.set(setting_key, original)


def test_prepare_command_keeps_unquoted_value_type_when_key_is_quoted():
    settings = get_settings()
    setting_key = "PR_CODE_SUGGESTIONS.NUM_CODE_SUGGESTIONS"
    original = settings.get(setting_key)

    try:
        command = pr_agent_module.prepare_command(
            '/review --"pr_code_suggestions.num_code_suggestions"=3'
        )

        assert command[0] == "/review"
        assert settings.get(setting_key) == original
        assert update_settings_from_args(command[1:]) == []
        assert settings.get(setting_key) == 3
        assert isinstance(settings.get(setting_key), int)
    finally:
        settings.set(setting_key, original)


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
async def test_incomplete_github_files_constructor_error_publishes_sanitized_notice(monkeypatch):
    secret = "private/repo mismatch: expected 5001 files but fetched 3000"
    provider = _incomplete_files_provider()

    class IncompleteTool:
        def __init__(self, pr_url, ai_handler, args):
            raise pr_agent_module.IncompletePullRequestFilesError(secret)

    _patch_request_dependencies(monkeypatch)
    monkeypatch.setattr(get_settings().config, "publish_output", True, raising=False)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)
    monkeypatch.setitem(pr_agent_module.command2class, "custom", IncompleteTool)

    handled = await pr_agent_module.PRAgent()._handle_request(
        "https://example/pr/1", "/custom"
    )

    assert handled is False
    provider.publish_comment.assert_called_once()
    published = provider.publish_comment.call_args.args[0]
    assert "GitHub returned an incomplete or inconsistent changed-file set" in published
    assert "If this pull request changes more than 3,000 files" in published
    assert "Otherwise, retry the command" in published
    assert "command was not run" in published
    assert secret not in published
    assert pr_agent_module.INCOMPLETE_GITHUB_FILES_COMMENT_MARKER in published.splitlines()[:5]


@pytest.mark.asyncio
async def test_unexpected_constructor_error_does_not_publish_incomplete_files_notice(monkeypatch):
    provider_factory = Mock()

    class BrokenTool:
        def __init__(self, pr_url, ai_handler, args):
            raise RuntimeError("unrelated")

    _patch_request_dependencies(monkeypatch)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", provider_factory)
    monkeypatch.setitem(pr_agent_module.command2class, "custom", BrokenTool)

    handled = await pr_agent_module.PRAgent()._handle_request(
        "https://example/pr/1", "/custom"
    )

    assert handled is False
    provider_factory.assert_not_called()


def test_incomplete_files_notice_deduplicates_trusted_agent_comment(monkeypatch):
    existing = {
        "body": (
            "## Existing notice\n\n"
            f"{pr_agent_module.INCOMPLETE_GITHUB_FILES_COMMENT_MARKER}\n\nDetails"
        )
    }
    provider = _incomplete_files_provider([existing])
    provider.is_comment_authored_by_pr_agent.return_value = True
    monkeypatch.setattr(get_settings().config, "publish_output", True, raising=False)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)

    pr_agent_module.publish_incomplete_github_files_comment("https://example/pr/1")

    provider.publish_comment.assert_not_called()


def test_foreign_incomplete_files_marker_does_not_suppress_notice(monkeypatch):
    existing = {
        "body": (
            "## Spoofed notice\n\n"
            f"{pr_agent_module.INCOMPLETE_GITHUB_FILES_COMMENT_MARKER}\n\nDetails"
        )
    }
    provider = _incomplete_files_provider([existing])
    monkeypatch.setattr(get_settings().config, "publish_output", True, raising=False)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)

    pr_agent_module.publish_incomplete_github_files_comment("https://example/pr/1")

    provider.publish_comment.assert_called_once()


def test_incomplete_files_notice_fails_open_when_author_cannot_be_verified(monkeypatch):
    existing = {
        "body": (
            "## Existing notice\n\n"
            f"{pr_agent_module.INCOMPLETE_GITHUB_FILES_COMMENT_MARKER}\n\nDetails"
        )
    }
    provider = _incomplete_files_provider([existing])
    provider.is_comment_authored_by_pr_agent.side_effect = RuntimeError("identity unavailable")
    monkeypatch.setattr(get_settings().config, "publish_output", True, raising=False)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)

    pr_agent_module.publish_incomplete_github_files_comment("https://example/pr/1")

    provider.publish_comment.assert_called_once()


def test_incomplete_files_notice_fails_open_when_comment_lookup_fails(monkeypatch):
    provider = _incomplete_files_provider()
    provider.get_issue_comments_newest_first.side_effect = RuntimeError("lookup unavailable")
    monkeypatch.setattr(get_settings().config, "publish_output", True, raising=False)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)

    pr_agent_module.publish_incomplete_github_files_comment("https://example/pr/1")

    provider.publish_comment.assert_called_once()


def test_incomplete_files_notice_fails_open_when_comment_body_cannot_be_read(monkeypatch):
    provider = _incomplete_files_provider([object()])
    provider._get_comment_body.side_effect = RuntimeError("comment decoding failed")
    monkeypatch.setattr(get_settings().config, "publish_output", True, raising=False)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)

    pr_agent_module.publish_incomplete_github_files_comment("https://example/pr/1")

    provider.publish_comment.assert_called_once()


@pytest.mark.asyncio
async def test_incomplete_files_notice_publication_failure_keeps_command_failed(monkeypatch):
    provider = _incomplete_files_provider()
    provider.publish_comment.side_effect = RuntimeError("publication unavailable")

    class IncompleteTool:
        def __init__(self, pr_url, ai_handler, args):
            raise pr_agent_module.IncompletePullRequestFilesError("internal details")

    _patch_request_dependencies(monkeypatch)
    monkeypatch.setattr(get_settings().config, "publish_output", True, raising=False)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)
    monkeypatch.setitem(pr_agent_module.command2class, "custom", IncompleteTool)

    handled = await pr_agent_module.PRAgent()._handle_request(
        "https://example/pr/1", "/custom"
    )

    assert handled is False
    provider.publish_comment.assert_called_once()


def test_incomplete_files_notice_respects_disabled_output(monkeypatch):
    provider_factory = Mock()
    monkeypatch.setattr(get_settings().config, "publish_output", False, raising=False)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", provider_factory)

    pr_agent_module.publish_incomplete_github_files_comment("https://example/pr/1")

    provider_factory.assert_not_called()


@pytest.mark.asyncio
async def test_handle_request_wrapper_returns_false_on_exception(monkeypatch):
    async def raise_error(self, pr_url, request, notify=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(pr_agent_module.PRAgent, "_handle_request", raise_error)

    handled = await pr_agent_module.PRAgent().handle_request("https://example/pr/1", "/review")

    assert handled is False


@pytest.mark.asyncio
async def test_handle_request_propagates_cancellation(monkeypatch):
    started = asyncio.Event()

    class BlockingTool:
        def __init__(self, pr_url, ai_handler, args):
            pass

        async def run(self):
            started.set()
            await asyncio.Event().wait()

    _patch_request_dependencies(monkeypatch)
    monkeypatch.setitem(
        pr_agent_module.command2class, "blocking", BlockingTool
    )

    task = asyncio.create_task(
        pr_agent_module.PRAgent(ai_handler="fake-ai").handle_request(
            "https://example/pr/1", "/blocking"
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


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
async def test_auto_review_reapplies_prepared_artifact_without_notifying(monkeypatch, tmp_path):
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("AUTO_REVIEW_ARTIFACT", encoding="utf-8")
    settings = get_settings()
    original_artifacts = settings.get("ARTIFACTS")
    original_instructions = settings.pr_reviewer.extra_instructions
    observed = []
    notify = Mock()

    class FakeReviewer:
        def __init__(self, _pr_url, is_answer=False, is_auto=False, args=None, ai_handler=None):
            observed.append((is_answer, is_auto, args, str(settings.pr_reviewer.extra_instructions)))

        async def run(self):
            return None

    try:
        settings.set("ARTIFACTS", {
            "enable": True,
            "artifact_path": str(artifact),
            "target_tools": ["pr_reviewer"],
        }, merge=False)
        settings.set("PR_REVIEWER.EXTRA_INSTRUCTIONS", "Base instructions")
        monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
        _patch_request_dependencies(
            monkeypatch,
            update_settings_fn=lambda args: ["--kept"],
        )

        def replace_instructions(_pr_url):
            settings.set("PR_REVIEWER.EXTRA_INSTRUCTIONS", "Repository instructions")

        monkeypatch.setattr(pr_agent_module, "apply_repo_settings", replace_instructions)
        monkeypatch.setattr(pr_agent_module, "PRReviewer", FakeReviewer)

        artifacts.inject_artifact_context()
        handled = await pr_agent_module.PRAgent(ai_handler="fake-ai")._handle_request(
            "https://example/pr/1", "/auto_review", notify
        )

        assert handled is True
        assert [(is_answer, is_auto, args) for is_answer, is_auto, args, _text in observed] == [
            (False, True, ["--kept"])
        ]
        assert observed[0][3].startswith("Repository instructions")
        assert observed[0][3].count("AUTO_REVIEW_ARTIFACT") == 1
        notify.assert_not_called()
    finally:
        settings.set("ARTIFACTS", original_artifacts, merge=False)
        settings.set("PR_REVIEWER.EXTRA_INSTRUCTIONS", original_instructions)


@pytest.mark.asyncio
async def test_unscoped_dispatcher_does_not_load_artifact_or_change_instructions(monkeypatch):
    settings = get_settings()
    original_instructions = settings.pr_reviewer.extra_instructions
    observed = []

    class FakeTool:
        def __init__(self, _pr_url, ai_handler=None, args=None):
            observed.append(str(settings.pr_reviewer.extra_instructions))

        async def run(self):
            return None

    def fail_if_loaded():
        pytest.fail("dispatcher without an ingress must not load an artifact")

    token = artifacts._artifact_context.set(None)
    try:
        settings.set("PR_REVIEWER.EXTRA_INSTRUCTIONS", "Unscoped instructions")
        _patch_request_dependencies(monkeypatch)
        monkeypatch.setattr(artifacts, "load_artifact", fail_if_loaded)
        monkeypatch.setitem(pr_agent_module.command2class, "review", FakeTool)

        handled = await pr_agent_module.PRAgent()._handle_request("https://example/pr/1", "/review")

        assert handled is True
        assert observed == ["Unscoped instructions"]
    finally:
        settings.set("PR_REVIEWER.EXTRA_INSTRUCTIONS", original_instructions)
        artifacts._artifact_context.reset(token)


@pytest.mark.asyncio
async def test_handle_request_returns_false_for_unknown_command(monkeypatch):
    _patch_request_dependencies(monkeypatch)

    handled = await pr_agent_module.PRAgent()._handle_request("https://example/pr/1", "/unknown")

    assert handled is False


@pytest.mark.asyncio
async def test_handle_request_language_instruction_preserves_control_values(monkeypatch):
    settings = get_settings()
    original = {
        key: settings.get(key).extra_instructions
        for key in settings
        if hasattr(settings.get(key), "extra_instructions")
    }
    settings.config.response_language = "de-DE"

    class FakeReviewer:
        def __init__(self, pr_url, is_answer=False, is_auto=False, args=None, ai_handler=None):
            pass

        async def run(self):
            pass

    try:
        _patch_request_dependencies(monkeypatch)
        monkeypatch.setattr(pr_agent_module, "PRReviewer", FakeReviewer)

        await pr_agent_module.PRAgent()._handle_request("https://example/pr/1", "/review")

        instructions = str(settings.pr_reviewer.extra_instructions)
        assert "de-DE" in instructions
        assert "Keep schema control values" in instructions
        assert "'No'" in instructions
        assert "do not translate them" in instructions
    finally:
        settings.config.response_language = "en-us"
        for key, value in original.items():
            settings.get(key).extra_instructions = value
