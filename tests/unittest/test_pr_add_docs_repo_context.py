"""Focused regression coverage for optional /add_docs repository instructions."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette_context import request_cycle_context

import pr_agent.commands.pr_add_docs as add_docs_module
from pr_agent.algo.pr_processing import FallbackEligibleError
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings

GUIDANCE = "Document public functions with a short usage example."
PATCH = "@@ -0,0 +1,2 @@\n+def example():\n+    return 1\n"


class _Provider(MagicMock):
    def get_repo_file_content(self, file_path, from_default_branch=False):
        return self.repo_file_fetch(file_path, from_default_branch=from_default_branch)


@pytest.fixture
def docs_settings():
    get_settings().as_dict()
    with request_cycle_context({"settings": deepcopy(get_settings())}):
        settings = get_settings()
        for key, value in {
            "config.model": "gpt-4o",
            "config.model_weak": "",
            "config.fallback_models": [],
            "config.max_model_tokens": 8_000,
            "config.publish_output": False,
            "config.repo_context_files": ["AGENTS.md"],
            "config.repo_context_from_default_branch": True,
            "config.repo_context_expand_imports": False,
        }.items():
            settings.set(key, value)
        yield settings


def _make_command(monkeypatch, instructions=GUIDANCE):
    provider = _Provider()
    provider.pr.title = "Add a helper"
    provider.get_languages.return_value = {"Python": 100}
    provider.get_files.return_value = ["src/app.py"]
    provider.get_pr_description.return_value = "Add an undocumented function"
    provider.get_pr_branch.return_value = "add-helper"
    provider.get_commit_messages.return_value = "Add a helper"
    provider.get_pr_url.return_value = "https://example.test/org/repo/pull/777"
    provider.get_repo_context_ref.return_value = None
    provider.repo_file_fetch.return_value = instructions
    provider.get_filtered_diff_file_names.return_value = []
    provider.get_diff_files.return_value = [
        FilePatchInfo("", "def example():\n    return 1\n", PATCH, "src/app.py", edit_type=EDIT_TYPE.ADDED)
    ]
    handler = SimpleNamespace(chat_completion=AsyncMock(return_value=("Code Documentation: []", "stop")))
    monkeypatch.setattr(add_docs_module, "get_git_provider_for_command", lambda _url: provider)
    command = add_docs_module.PRAddDocs("https://example.test/org/repo/pull/777", ai_handler=lambda: handler)
    return command, provider, handler


@pytest.mark.parametrize(
    "override, expected",
    [
        (None, True),
        (True, True),
        (1, True),
        ("true", True),
        (False, False),
        (0, False),
        ("false", False),
        ("invalid", False),
    ],
)
async def test_default_instructions_and_opt_out(monkeypatch, docs_settings, override, expected):
    if override is not None:
        docs_settings.set("pr_add_docs.use_repo_context", override)
    command, provider, handler = _make_command(monkeypatch)

    await command.run()

    request = handler.chat_completion.await_args.kwargs
    assert (GUIDANCE in request["system"]) is expected
    assert "+def example():" in request["user"]
    assert handler.chat_completion.await_count == 1
    assert command._attempt_uses_repo_context is expected
    if expected:
        provider.repo_file_fetch.assert_called_once_with("AGENTS.md", from_default_branch=True)
    else:
        provider.repo_file_fetch.assert_not_called()


async def test_empty_configuration_or_missing_file_preserves_diff(monkeypatch, docs_settings):
    docs_settings.set("config.repo_context_files", [])
    command, provider, handler = _make_command(monkeypatch)
    await command.run()
    provider.repo_file_fetch.assert_not_called()
    assert "+def example():" in handler.chat_completion.await_args.kwargs["user"]

    docs_settings.set("config.repo_context_files", ["AGENTS.md"])
    command, provider, handler = _make_command(monkeypatch, instructions="")
    await command.run()
    assert GUIDANCE not in handler.chat_completion.await_args.kwargs["system"]
    assert "+def example():" in handler.chat_completion.await_args.kwargs["user"]


async def test_failed_context_fetch_is_optional(monkeypatch, docs_settings):
    command, provider, handler = _make_command(monkeypatch)
    provider.repo_file_fetch.side_effect = OSError("unavailable")

    await command.run()

    assert "+def example():" in handler.chat_completion.await_args.kwargs["user"]
    assert command._attempt_uses_repo_context is False


async def test_partial_context_failure_is_omitted_and_retried(monkeypatch, docs_settings):
    docs_settings.set("config.repo_context_files", ["AGENTS.md", "STYLE.md"])
    command, provider, handler = _make_command(monkeypatch)
    available = False

    def fetch(file_path, *, from_default_branch):
        assert from_default_branch is True
        if file_path == "STYLE.md":
            if not available:
                raise OSError("temporarily unavailable")
            return "Use short docstrings."
        return GUIDANCE

    provider.repo_file_fetch.side_effect = fetch
    await command._prepare_prediction("gpt-4o")
    assert GUIDANCE not in handler.chat_completion.await_args.kwargs["system"]
    assert "+def example():" in handler.chat_completion.await_args.kwargs["user"]
    assert command._repo_context is None

    available = True
    await command._prepare_prediction("gpt-4o-mini")
    assert GUIDANCE in handler.chat_completion.await_args.kwargs["system"]
    assert "Use short docstrings." in handler.chat_completion.await_args.kwargs["system"]
    assert provider.repo_file_fetch.call_count == 4


async def test_unfittable_context_is_omitted_without_truncating_diff(monkeypatch, docs_settings):
    command, _, handler = _make_command(monkeypatch)
    monkeypatch.setattr(
        add_docs_module, "build_repo_context",
        lambda *_args, **_kwargs: ("long instructions " * 20_000, True),
    )

    await command.run()

    request = handler.chat_completion.await_args.kwargs
    assert "long instructions" not in request["system"]
    assert "+def example():" in request["user"]
    assert command._attempt_uses_repo_context is False


@pytest.mark.parametrize(
    "custom_user",
    [
        "Patch: {{ diff | e }}",
        "Patch: {{ diff | tojson }}",
        "{% if repo_context is defined %}unexpected{% else %}{{ diff | trim }}{% endif %}",
    ],
)
async def test_custom_jinja_rendering_unchanged(monkeypatch, docs_settings, custom_user):
    docs_settings.set("pr_add_docs_prompt.system", "Custom style: {{ docs_for_language }} / {{ extra_instructions }}")
    docs_settings.set("pr_add_docs_prompt.user", custom_user)
    docs_settings.set("pr_add_docs.extra_instructions", "Keep it brief")
    docs_settings.set("pr_add_docs.docs_style", "PEP257")

    docs_settings.set("pr_add_docs.use_repo_context", False)
    baseline, _, baseline_handler = _make_command(monkeypatch)
    await baseline.run()
    baseline_request = baseline_handler.chat_completion.await_args.kwargs

    docs_settings.set("pr_add_docs.use_repo_context", True)
    guided, _, guided_handler = _make_command(monkeypatch)
    await guided.run()
    guided_request = guided_handler.chat_completion.await_args.kwargs

    assert guided_request["user"] == baseline_request["user"]
    assert guided_request["system"].startswith(baseline_request["system"])
    assert GUIDANCE in guided_request["system"]
    assert "PEP257" in guided_request["system"]
    assert "Keep it brief" in guided_request["system"]
    assert "unexpected" not in guided_request["user"]


async def test_complete_context_reused_across_model_attempts(monkeypatch, docs_settings):
    command, provider, handler = _make_command(monkeypatch)

    await command._prepare_prediction("gpt-4o")
    await command._prepare_prediction("gpt-4o-mini")

    assert handler.chat_completion.await_count == 2
    provider.repo_file_fetch.assert_called_once()
    assert command._attempt_uses_repo_context is True


async def test_skip_guidance_when_no_analyzable_diff(monkeypatch, docs_settings):
    command, provider, handler = _make_command(monkeypatch)
    monkeypatch.setattr(add_docs_module, "get_pr_diff", lambda *_args, **_kwargs: ("", []))

    await command._prepare_prediction("gpt-4o")

    handler.chat_completion.assert_not_awaited()
    provider.repo_file_fetch.assert_not_called()


def test_sensitive_invalid_yaml_does_not_log_response(monkeypatch, docs_settings):
    command, _, _ = _make_command(monkeypatch)
    logs = MagicMock()
    monkeypatch.setattr(add_docs_module, "get_logger", lambda: logs)
    monkeypatch.setattr("pr_agent.algo.utils.get_logger", lambda: logs)
    command._attempt_uses_repo_context = True
    command.prediction = "PRIVATE_GUIDANCE: : [broken"

    with pytest.raises(FallbackEligibleError):
        command._prepare_pr_code_docs()

    assert "PRIVATE_GUIDANCE" not in str(logs.mock_calls)


async def test_guided_publication_failure_logs_stage_without_provider_message(monkeypatch, docs_settings):
    docs_settings.set("config.publish_output", True)
    command, provider, handler = _make_command(monkeypatch)
    logs = MagicMock()
    monkeypatch.setattr(add_docs_module, "get_logger", lambda: logs)
    handler.chat_completion.return_value = (
        "Code Documentation:\n"
        "  - relevant file: src/app.py\n"
        "    relevant line: 1\n"
        "    doc placement: before\n"
        "    documentation: Document the helper.\n",
        "stop",
    )
    provider.remove_initial_comment.side_effect = RuntimeError("SECRET_PROVIDER_RESPONSE")

    await command.run()

    assert command._attempt_uses_repo_context is True
    assert "removing the progress comment" in logs.error.call_args.args[0]
    assert "RuntimeError" in logs.error.call_args.args[0]
    assert "SECRET_PROVIDER_RESPONSE" not in str(logs.error.call_args)
    assert "SECRET_PROVIDER_RESPONSE" not in str(logs.warning.call_args)
