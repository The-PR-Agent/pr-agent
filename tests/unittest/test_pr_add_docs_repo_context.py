"""Exercise the real /add_docs diff and model path with repository instructions."""

import asyncio
from copy import deepcopy
from threading import get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest
from starlette_context import request_cycle_context

import pr_agent.algo.pr_processing as pr_processing_module
import pr_agent.algo.repo_context as repo_context_module
import pr_agent.algo.utils as utils_module
import pr_agent.commands.pr_add_docs as add_docs_module
from pr_agent.algo.ai_handlers.litellm_ai_handler import _PROMPT_LOGS_WITHHELD
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
            "config.repo_context_sibling_repos": [],
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
    [(None, True), (True, True), (False, False), ("false", False), ("invalid", True)],
)
async def test_add_docs_repository_instructions_default_on_with_opt_out(
    monkeypatch, docs_settings, override, expected
):
    if override is not None:
        docs_settings.set("pr_add_docs.use_repo_context", override)
    command, provider, handler = _make_command(monkeypatch)

    await command.run()

    [request] = handler.chat_completion.await_args_list
    system, user = request.kwargs["system"], request.kwargs["user"]
    assert (GUIDANCE in system) is expected
    assert "+def example():" in user
    if expected:
        provider.repo_file_fetch.assert_called_once_with("AGENTS.md", from_default_branch=True)
    else:
        provider.repo_file_fetch.assert_not_called()


@pytest.mark.parametrize("placement", ["absent", "system", "user"])
async def test_add_docs_custom_prompts_include_repository_guidance_once(monkeypatch, docs_settings, placement):
    system_template = "Custom documentation style: concise."
    user_template = "Patch:\n{{ diff }}"
    if placement == "system":
        system_template += "\nRepository guidance: {{ repo_context }}"
    elif placement == "user":
        user_template += "\nRepository guidance: {{ repo_context }}"
    docs_settings.set("pr_add_docs_prompt.system", system_template)
    docs_settings.set("pr_add_docs_prompt.user", user_template)
    command, provider, handler = _make_command(monkeypatch)

    await command.run()

    request = handler.chat_completion.await_args.kwargs
    assert request["system"].count(GUIDANCE) + request["user"].count(GUIDANCE) == 1
    assert "+def example():" in request["user"]
    if placement == "absent":
        assert request["system"].startswith("Repository instructions")
        assert request["system"].endswith(system_template)
    provider.repo_file_fetch.assert_called_once()


async def test_add_docs_custom_prompt_without_slot_respects_opt_out(monkeypatch, docs_settings):
    docs_settings.set("pr_add_docs.use_repo_context", False)
    docs_settings.set("pr_add_docs_prompt.system", "Custom documentation style: concise.")
    docs_settings.set("pr_add_docs_prompt.user", "Patch:\n{{ diff }}")
    command, provider, handler = _make_command(monkeypatch)

    await command.run()

    request = handler.chat_completion.await_args.kwargs
    assert request["system"] == "Custom documentation style: concise."
    assert GUIDANCE not in str(request)
    provider.repo_file_fetch.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
async def test_add_docs_fills_missing_optional_repo_context_variable(monkeypatch, docs_settings, enabled):
    docs_settings.set("pr_add_docs.use_repo_context", enabled)
    command, provider, handler = _make_command(monkeypatch)
    del command.vars["repo_context"]

    await command.run()

    handler.chat_completion.assert_awaited_once()
    request = handler.chat_completion.await_args.kwargs
    assert (GUIDANCE in request["system"]) is enabled
    assert "+def example():" in request["user"]
    assert provider.repo_file_fetch.call_count == int(enabled)


@pytest.mark.parametrize("custom_prompt", [False, True])
async def test_add_docs_omits_oversized_instructions_without_shortening_diff(monkeypatch, docs_settings, custom_prompt):
    if custom_prompt:
        docs_settings.set("pr_add_docs_prompt.system", "Custom documentation style: concise.")
        docs_settings.set("pr_add_docs_prompt.user", "Patch:\n{{ diff }}")
    docs_settings.set("pr_add_docs.use_repo_context", False)
    baseline, _, baseline_handler = _make_command(monkeypatch)
    await baseline.run()
    [baseline_request] = baseline_handler.chat_completion.await_args_list

    docs_settings.set("pr_add_docs.use_repo_context", True)
    command, provider, handler = _make_command(monkeypatch, "documentation convention " * 20_000)
    await command.run()

    [request] = handler.chat_completion.await_args_list
    assert request.kwargs == baseline_request.kwargs
    assert "+def example():" in request.kwargs["user"]
    assert "return 1" in request.kwargs["user"]
    provider.repo_file_fetch.assert_called_once()


async def test_add_docs_reuses_repo_instructions_across_model_fallback(monkeypatch, docs_settings):
    docs_settings.set("pr_add_docs.use_repo_context", True)
    docs_settings.set("config.fallback_models", ["gpt-4o-mini"])
    command, provider, handler = _make_command(monkeypatch)
    context_builder = MagicMock(wraps=add_docs_module.build_repo_context)
    monkeypatch.setattr(add_docs_module, "build_repo_context", context_builder)
    handler.chat_completion.side_effect = [
        ("Code Documentation: []", "length"),
        ("Code Documentation: []", "stop"),
    ]

    await command.run()

    assert [call.kwargs["model"] for call in handler.chat_completion.await_args_list] == [
        "gpt-4o", "gpt-4o-mini"
    ]
    assert all(GUIDANCE in call.kwargs["system"] for call in handler.chat_completion.await_args_list)
    context_builder.assert_called_once()
    provider.repo_file_fetch.assert_called_once()


async def test_add_docs_retries_incomplete_repo_context_on_fallback(monkeypatch, docs_settings):
    docs_settings.set("config.fallback_models", ["gpt-4o-mini"])
    docs_settings.set("config.repo_context_files", ["AGENTS.md", "STYLE.md"])
    command, provider, handler = _make_command(monkeypatch)
    style_guidance = "Use concise parameter descriptions."
    provider.repo_file_fetch.side_effect = [
        GUIDANCE, OSError("temporary file read failure"), GUIDANCE, style_guidance
    ]
    handler.chat_completion.side_effect = [
        ("Code Documentation: []", "length"),
        ("Code Documentation: []", "stop"),
    ]

    await command.run()

    first, second = handler.chat_completion.await_args_list
    assert GUIDANCE in first.kwargs["system"]
    assert style_guidance not in first.kwargs["system"]
    assert GUIDANCE in second.kwargs["system"]
    assert style_guidance in second.kwargs["system"]
    assert [call.args[0] for call in provider.repo_file_fetch.call_args_list] == [
        "AGENTS.md", "STYLE.md", "AGENTS.md", "STYLE.md"
    ]


async def test_add_docs_import_fetch_errors_withhold_instruction_paths(monkeypatch, docs_settings):
    docs_settings.set("config.repo_context_expand_imports", True)
    command, provider, handler = _make_command(monkeypatch)
    imported_path = "private/repository-conventions.md"
    provider_error = f"rejected {imported_path} containing {GUIDANCE}"
    request_thread = get_ident()
    fetch_threads = set()

    def fetch(path, from_default_branch=False):
        fetch_threads.add(get_ident())
        assert _PROMPT_LOGS_WITHHELD.get()
        if path == "AGENTS.md":
            return f"{GUIDANCE}\n@{imported_path}"
        raise OSError(provider_error)

    provider.repo_file_fetch.side_effect = fetch
    logger = MagicMock()
    monkeypatch.setattr(repo_context_module, "get_logger", lambda: logger)

    await command.run()

    handler.chat_completion.assert_awaited_once()
    assert provider.repo_file_fetch.call_count == 2
    assert fetch_threads and request_thread not in fetch_threads
    assert imported_path not in str(logger.mock_calls)
    assert provider_error not in str(logger.mock_calls)
    assert "Failed to load repo context import" in str(logger.mock_calls)
    assert not _PROMPT_LOGS_WITHHELD.get()


async def test_add_docs_reuses_successful_empty_context_on_fallback(monkeypatch, docs_settings):
    docs_settings.set("config.fallback_models", ["gpt-4o-mini"])
    command, provider, handler = _make_command(monkeypatch, "")
    context_builder = MagicMock(wraps=add_docs_module.build_repo_context)
    monkeypatch.setattr(add_docs_module, "build_repo_context", context_builder)
    handler.chat_completion.side_effect = [
        ("Code Documentation: []", "length"),
        ("Code Documentation: []", "stop"),
    ]

    await command.run()

    assert handler.chat_completion.await_count == 2
    context_builder.assert_called_once()
    provider.repo_file_fetch.assert_called_once()


async def test_add_docs_does_not_log_repository_guidance(monkeypatch, docs_settings):
    docs_settings.set("pr_add_docs_prompt.user", docs_settings.pr_add_docs_prompt.user + "\n{{ repo_context }}")
    command, _, handler = _make_command(monkeypatch)
    logger = MagicMock()
    monkeypatch.setattr(add_docs_module, "get_verbosity_level", lambda: 2)
    monkeypatch.setattr(add_docs_module, "get_logger", lambda: logger)

    async def predict(**kwargs):
        assert _PROMPT_LOGS_WITHHELD.get()
        assert GUIDANCE in kwargs["system"]
        assert GUIDANCE in kwargs["user"]
        return "Code Documentation: []", "stop"

    handler.chat_completion.side_effect = predict
    await command.run()

    logged = "\n".join(str(call.args[0]) for call in logger.info.call_args_list)
    assert GUIDANCE not in logged
    assert "Prompts withheld" in logged
    assert not _PROMPT_LOGS_WITHHELD.get()


@pytest.mark.parametrize("use_repo_context", [False, True])
@pytest.mark.parametrize(
    "prediction",
    [
        f"unexpected: {GUIDANCE}",
        f"unexpected: [{GUIDANCE}",
    ],
)
async def test_add_docs_malformed_prediction_withholds_guidance_from_logs(
    monkeypatch, docs_settings, use_repo_context, prediction
):
    docs_settings.set("pr_add_docs.use_repo_context", use_repo_context)
    command, _, handler = _make_command(monkeypatch)
    handler.chat_completion.return_value = (prediction, "stop")
    logger = MagicMock()
    monkeypatch.setattr(add_docs_module, "get_logger", lambda: logger)
    monkeypatch.setattr(utils_module, "get_logger", lambda: logger)

    await command.run()

    logged = "\n".join(str(call) for call in logger.mock_calls)
    assert (GUIDANCE in logged) is not use_repo_context
    assert handler.chat_completion.await_count == 1


@pytest.mark.parametrize("enabled", [False, True])
async def test_add_docs_publication_scopes_log_withholding(monkeypatch, docs_settings, enabled):
    docs_settings.set("pr_add_docs.use_repo_context", enabled)
    docs_settings.set("config.publish_output", True)
    command, _, _ = _make_command(monkeypatch)
    observed = []

    def publish(_data):
        observed.append(_PROMPT_LOGS_WITHHELD.get())
        return True

    command.push_inline_docs = MagicMock(side_effect=publish)
    await command.run()

    assert observed == [enabled]
    assert not _PROMPT_LOGS_WITHHELD.get()


async def test_add_docs_sanitizes_provider_errors_before_fallback(monkeypatch, docs_settings):
    docs_settings.set("config.fallback_models", ["gpt-4o-mini"])
    command, _, handler = _make_command(monkeypatch)
    handler.chat_completion.side_effect = [
        openai.APIError(
            f"Rejected prompt: {GUIDANCE}",
            request=httpx.Request("POST", "https://example.test/llm"),
            body=None,
        ),
        ("Code Documentation: []", "stop"),
    ]
    logger = MagicMock()
    monkeypatch.setattr(add_docs_module, "get_logger", lambda: logger)
    monkeypatch.setattr(pr_processing_module, "get_logger", lambda: logger)

    await command.run()

    assert handler.chat_completion.await_count == 2
    assert GUIDANCE not in str(logger.mock_calls)


async def test_add_docs_hides_unexpected_provider_errors_with_propagation(monkeypatch, docs_settings):
    docs_settings.set("config.propagate_command_errors", True)
    command, _, handler = _make_command(monkeypatch)
    handler.chat_completion.side_effect = RuntimeError(f"Rejected prompt: {GUIDANCE}")
    logger = MagicMock()
    monkeypatch.setattr(add_docs_module, "get_logger", lambda: logger)

    with pytest.raises(RuntimeError, match="details withheld") as exc:
        await command.run()

    assert exc.value.__suppress_context__
    assert GUIDANCE not in str(exc.value)
    assert GUIDANCE not in str(logger.mock_calls)


def test_add_docs_redacted_error_handles_nonstandard_exception_constructors():
    class ProviderError(Exception):
        def __init__(self, details):
            super().__init__(details.get("message"))

    error = ProviderError({"message": GUIDANCE})
    sanitized = add_docs_module._redacted_error(error)

    assert isinstance(sanitized, RuntimeError)
    assert "details withheld" in str(sanitized)
    assert GUIDANCE not in str(sanitized)


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
async def test_add_docs_preserves_nonretryable_error_types_without_leaking_details(
    monkeypatch, docs_settings, error_type
):
    docs_settings.set("config.propagate_command_errors", True)
    docs_settings.set("config.fallback_models", ["gpt-4o-mini"])
    command, _, handler = _make_command(monkeypatch)
    handler.chat_completion.side_effect = error_type(f"Rejected prompt: {GUIDANCE}")
    logger = MagicMock()
    monkeypatch.setattr(add_docs_module, "get_logger", lambda: logger)

    with pytest.raises(error_type, match="details withheld") as raised:
        await command.run()

    assert GUIDANCE not in str(raised.value)
    assert GUIDANCE not in str(logger.mock_calls)
    assert handler.chat_completion.await_count == 1
    assert raised.value.__suppress_context__


@pytest.mark.parametrize("error_type", [openai.APIError, asyncio.TimeoutError])
async def test_add_docs_preserves_retry_for_known_provider_failures(monkeypatch, docs_settings, error_type):
    docs_settings.set("config.fallback_models", ["gpt-4o-mini"])
    command, _, handler = _make_command(monkeypatch)
    error = (
        error_type(
            f"Rejected prompt: {GUIDANCE}",
            request=httpx.Request("POST", "https://example.test/llm"),
            body=None,
        )
        if error_type is openai.APIError
        else error_type(f"Rejected prompt: {GUIDANCE}")
    )
    handler.chat_completion.side_effect = [error, ("Code Documentation: []", "stop")]

    await command.run()

    assert handler.chat_completion.await_count == 2


@pytest.mark.parametrize(
    "error_type, status",
    [(openai.AuthenticationError, 401), (openai.PermissionDeniedError, 403)],
)
async def test_add_docs_preserves_alternative_model_fallback_after_auth_error(
    monkeypatch, docs_settings, error_type, status
):
    # An alternative model may use different provider credentials or permissions.
    docs_settings.set("config.fallback_models", ["gpt-4o-mini"])
    command, _, handler = _make_command(monkeypatch)
    request = httpx.Request("POST", "https://example.test/llm")
    response = httpx.Response(status, request=request)
    error = error_type(f"Rejected prompt: {GUIDANCE}", response=response, body=None)
    handler.chat_completion.side_effect = [error, ("Code Documentation: []", "stop")]
    logger = MagicMock()
    monkeypatch.setattr(add_docs_module, "get_logger", lambda: logger)
    monkeypatch.setattr(pr_processing_module, "get_logger", lambda: logger)

    await command.run()

    assert handler.chat_completion.await_count == 2
    assert GUIDANCE not in str(logger.mock_calls)


async def test_add_docs_publication_error_preserves_type_without_exposing_response(monkeypatch, docs_settings):
    docs_settings.set("config.propagate_command_errors", True)
    docs_settings.set("config.publish_output", True)
    command, provider, handler = _make_command(monkeypatch)
    handler.chat_completion.return_value = ("Code Documentation: []", "stop")
    command.push_inline_docs = MagicMock(side_effect=ValueError(f"Published guidance: {GUIDANCE}"))
    logger = MagicMock()
    monkeypatch.setattr(add_docs_module, "get_logger", lambda: logger)

    with pytest.raises(ValueError, match="details withheld") as raised:
        await command.run()

    assert GUIDANCE not in str(raised.value)
    assert GUIDANCE not in str(logger.mock_calls)
    assert command.push_inline_docs.call_count == 1


@pytest.mark.parametrize("uses_repo_context", [True, False])
async def test_add_docs_cleanup_error_respects_instruction_guard(monkeypatch, docs_settings, uses_repo_context):
    docs_settings.set("config.publish_output", True)
    docs_settings.set("pr_add_docs.use_repo_context", uses_repo_context)
    command, provider, handler = _make_command(monkeypatch)
    handler.chat_completion.side_effect = ValueError("Model request failed")
    provider.remove_initial_comment.side_effect = RuntimeError(f"Provider echoed: {GUIDANCE}")
    logger = MagicMock()
    monkeypatch.setattr(add_docs_module, "get_logger", lambda: logger)

    await command.run()

    provider.remove_initial_comment.assert_called_once()
    assert "temporary documentation comment" in str(logger.mock_calls)
    assert (GUIDANCE in str(logger.mock_calls)) is not uses_repo_context


async def test_add_docs_withheld_api_error_keeps_safe_diagnostics(monkeypatch, docs_settings):
    command, _, handler = _make_command(monkeypatch)
    command._attempt_system_prompt = GUIDANCE
    command._attempt_user_prompt = "Normal prompt"
    command._attempt_uses_repo_context = True
    request = httpx.Request("POST", "https://example.test/llm")
    handler.chat_completion.side_effect = openai.BadRequestError(
        GUIDANCE,
        response=httpx.Response(400, request=request),
        body={"code": "model_not_found"},
    )

    with pytest.raises(add_docs_module.FallbackEligibleError) as raised:
        await command._get_prediction("gpt-4o")

    assert "BadRequestError, HTTP 400, code=model_not_found" in str(raised.value)
    assert GUIDANCE not in str(raised.value)


async def test_add_docs_skips_repo_context_builder_when_no_files_configured(monkeypatch, docs_settings):
    docs_settings.set("config.repo_context_files", [])
    command, provider, handler = _make_command(monkeypatch)
    context_builder = MagicMock(side_effect=AssertionError("no files should be fetched"))
    monkeypatch.setattr(add_docs_module, "build_repo_context", context_builder)

    await command.run()

    handler.chat_completion.assert_awaited_once()
    context_builder.assert_not_called()
    provider.repo_file_fetch.assert_not_called()


async def test_add_docs_does_not_fetch_instructions_without_analyzable_diff(monkeypatch, docs_settings):
    docs_settings.set("pr_add_docs.use_repo_context", True)
    command, provider, handler = _make_command(monkeypatch)
    provider.get_diff_files.return_value = []

    await command.run()

    handler.chat_completion.assert_not_called()
    provider.repo_file_fetch.assert_not_called()


def test_add_docs_direct_parsing_without_attempt_state():
    command = add_docs_module.PRAddDocs.__new__(add_docs_module.PRAddDocs)
    command.prediction = "Code Documentation: []"

    assert command._prepare_pr_code_docs() == {"Code Documentation": []}


@pytest.mark.parametrize("use_repo_context", [False, True])
def test_add_docs_dedent_error_withholds_model_generated_filename(monkeypatch, use_repo_context):
    command = add_docs_module.PRAddDocs.__new__(add_docs_module.PRAddDocs)
    command.git_provider = SimpleNamespace(diff_files=[object()])
    command._attempt_uses_repo_context = use_repo_context
    logger = MagicMock()
    monkeypatch.setattr(add_docs_module, "get_logger", lambda: logger)
    monkeypatch.setattr(add_docs_module, "get_verbosity_level", lambda: 2)
    model_filename = f"src/{GUIDANCE}.py"

    assert command.dedent_code(model_filename, 1, "Docstring", add_original_line=True) is None

    logged = str(logger.mock_calls)
    assert (GUIDANCE in logged) is not use_repo_context
    assert "Could not dedent code snippet" in logged


def test_add_docs_direct_publication_without_attempt_state(monkeypatch):
    command = add_docs_module.PRAddDocs.__new__(add_docs_module.PRAddDocs)
    command.git_provider = MagicMock()
    command.git_provider.publish_code_suggestions.return_value = True
    command.dedent_code = MagicMock(return_value='def example():\n    """Docstring."""\n    return 1')
    monkeypatch.setattr(add_docs_module, "get_verbosity_level", lambda: 2)
    entry = {
        "relevant file": "src/app.py",
        "relevant line": 1,
        "doc placement": "after",
        "documentation": '"""Docstring."""',
    }

    assert command.push_inline_docs({"Code Documentation": [entry]}) is True
    command.git_provider.publish_code_suggestions.assert_called_once()


async def test_add_docs_prediction_without_attempt_state(monkeypatch, docs_settings):
    command = add_docs_module.PRAddDocs.__new__(add_docs_module.PRAddDocs)
    command._attempt_system_prompt = "ordinary system"
    command._attempt_user_prompt = "ordinary user"
    command.ai_handler = SimpleNamespace(chat_completion=AsyncMock(return_value=("Code Documentation: []", "stop")))
    logger = MagicMock()
    monkeypatch.setattr(add_docs_module, "get_verbosity_level", lambda: 2)
    monkeypatch.setattr(add_docs_module, "get_logger", lambda: logger)

    assert await command._get_prediction("gpt-4o") == "Code Documentation: []"
    assert "ordinary system" in str(logger.mock_calls)
    assert not _PROMPT_LOGS_WITHHELD.get()
