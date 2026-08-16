import json
from unittest.mock import patch

import pytest

from pr_agent.algo.ai_handlers.cli_agent_ai_handler import (
    CliAgentAIHandler, CliAgentConfigError, CliAgentError, _claude_argv,
    _codex_argv, _diagnostic_tail, _parse_claude_output,
    _unwrap_structured_output, parse_cli_model)
from pr_agent.algo.ai_handlers.handler_factory import get_ai_handler_class
from pr_agent.config_loader import get_settings


class TestParseCliModel:
    @pytest.mark.parametrize("model,expected", [
        ("cli/claude/opus", ("claude", "opus")),
        ("cli/claude", ("claude", None)),
        ("cli/claude/", ("claude", None)),
        ("cli/codex/gpt-5.1-codex-max", ("codex", "gpt-5.1-codex-max")),
        ("  cli/CLAUDE/sonnet  ", ("claude", "sonnet")),
    ])
    def test_valid(self, model, expected):
        assert parse_cli_model(model) == expected

    @pytest.mark.parametrize("model", ["gpt-5.6", "", None, "cli/gemini/pro", "claude/opus"])
    def test_rejects_non_cli_or_unknown_backend(self, model):
        with pytest.raises(CliAgentConfigError):
            parse_cli_model(model)

    def test_model_may_contain_slashes(self):
        # Anything after the backend is passed through to --model verbatim.
        assert parse_cli_model("cli/codex/openai/gpt-5") == ("codex", "openai/gpt-5")


class TestArgv:
    def test_claude_argv_replaces_system_prompt_and_disables_tools(self):
        argv = _claude_argv("/usr/bin/claude", "opus", "SYSTEM TEXT")
        assert argv[0] == "/usr/bin/claude"
        assert "--print" in argv
        assert argv[argv.index("--output-format") + 1] == "json"
        assert argv[argv.index("--model") + 1] == "opus"
        assert "--safe-mode" in argv
        assert "--disallowed-tools" in argv
        system = argv[argv.index("--system-prompt") + 1]
        assert system.startswith("SYSTEM TEXT")
        # The output contract must ride along with the tool's own system prompt.
        assert "non-interactive text generator" in system

    def test_claude_argv_omits_model_when_unset(self):
        assert "--model" not in _claude_argv("/usr/bin/claude", None, "S")

    def test_codex_argv_reads_stdin_last(self):
        argv = _codex_argv("/usr/bin/codex", "gpt-5", "/tmp/out.txt")
        assert argv[1] == "exec"
        assert argv[-1] == "-", "the stdin marker must stay the trailing positional arg"
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert argv[argv.index("--output-last-message") + 1] == "/tmp/out.txt"

    def test_codex_argv_ignores_user_config_by_default(self):
        assert "--ignore-user-config" in _codex_argv("/usr/bin/codex", None, "/tmp/o")

    def test_codex_argv_can_keep_user_config(self):
        get_settings().set("CLI_AGENT.CODEX_IGNORE_USER_CONFIG", False)
        try:
            assert "--ignore-user-config" not in _codex_argv("/usr/bin/codex", None, "/tmp/o")
        finally:
            get_settings().set("CLI_AGENT.CODEX_IGNORE_USER_CONFIG", True)

    def test_extra_args_are_appended_before_stdin_marker(self):
        get_settings().set("CLI_AGENT.EXTRA_ARGS", ["--effort", "high"])
        try:
            argv = _codex_argv("/usr/bin/codex", None, "/tmp/out.txt")
            assert argv[-3:] == ["--effort", "high", "-"]
        finally:
            get_settings().set("CLI_AGENT.EXTRA_ARGS", [])


class TestParseClaudeOutput:
    def test_result_object(self):
        payload = json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "review: ok",
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 3},
        })
        text, finish_reason, usage = _parse_claude_output(payload)
        assert text == "review: ok"
        assert finish_reason == "stop"
        assert usage == {"prompt_tokens": 15, "completion_tokens": 3, "total_tokens": 18}

    def test_message_list_takes_last_result(self):
        payload = json.dumps([
            {"type": "assistant", "message": "ignored"},
            {"type": "result", "subtype": "success", "result": "final"},
        ])
        assert _parse_claude_output(payload)[0] == "final"

    def test_max_turns_reported_as_length(self):
        payload = json.dumps({"type": "result", "subtype": "error_max_turns", "result": "partial"})
        assert _parse_claude_output(payload)[1] == "length"

    def test_is_error_raises(self):
        payload = json.dumps({"type": "result", "is_error": True, "result": "boom"})
        with pytest.raises(CliAgentError, match="boom"):
            _parse_claude_output(payload)

    def test_non_json_raises(self):
        with pytest.raises(CliAgentError, match="non-JSON"):
            _parse_claude_output("Here is your review!")


class TestDiagnosticTail:
    def test_prefers_error_lines_over_echoed_prompt(self):
        # codex echoes the prompt to stderr; a plain tail slice would report diff text.
        stderr = "user\nreview this\n-def add(a, b): return a - b\nERROR: model not supported"
        assert _diagnostic_tail(stderr) == "ERROR: model not supported"

    def test_deduplicates_repeated_errors(self):
        stderr = "ERROR: boom\nERROR: boom"
        assert _diagnostic_tail(stderr) == "ERROR: boom"

    def test_falls_back_to_tail_when_nothing_is_flagged(self):
        assert _diagnostic_tail("just\nsome\nnoise") == "just\nsome\nnoise"

    def test_respects_limit(self):
        assert len(_diagnostic_tail("x" * 900)) == 500


class TestUnwrapStructuredOutput:
    def test_unwraps_when_prose_surrounds_a_single_fence(self):
        text = "Here's the review:\n\n```yaml\nkey: value\n```\n\nHope that helps!"
        assert _unwrap_structured_output(text) == "key: value\n"

    def test_leaves_clean_response_untouched(self):
        assert _unwrap_structured_output("key: value") == "key: value"

    def test_leaves_bare_fence_untouched(self):
        # Nothing outside the fence: load_yaml already strips this shape itself.
        text = "```yaml\nkey: value\n```"
        assert _unwrap_structured_output(text) == text

    def test_leaves_multi_block_prose_untouched(self):
        # An /ask answer legitimately mixes prose with several blocks; rewriting it would lose content.
        text = "First:\n```yaml\na: 1\n```\nSecond:\n```json\n{}\n```"
        assert _unwrap_structured_output(text) == text

    def test_disabled_by_setting(self):
        get_settings().set("CLI_AGENT.UNWRAP_STRUCTURED_OUTPUT", False)
        try:
            text = "prefix\n```yaml\nkey: value\n```"
            assert _unwrap_structured_output(text) == text
        finally:
            get_settings().set("CLI_AGENT.UNWRAP_STRUCTURED_OUTPUT", True)


class TestHandlerFactory:
    def teardown_method(self):
        get_settings().set("CONFIG.AI_HANDLER", "")
        get_settings().set("CONFIG.MODEL", "gpt-5.6")

    def test_cli_model_selects_cli_agent(self):
        get_settings().set("CONFIG.AI_HANDLER", "")
        get_settings().set("CONFIG.MODEL", "cli/claude/opus")
        assert get_ai_handler_class() is CliAgentAIHandler

    def test_default_is_litellm(self):
        pytest.importorskip("litellm", reason="litellm is a hard dependency; skipped only where it fails to build")
        from pr_agent.algo.ai_handlers.litellm_ai_handler import \
            LiteLLMAIHandler
        get_settings().set("CONFIG.AI_HANDLER", "")
        get_settings().set("CONFIG.MODEL", "gpt-5.6")
        assert get_ai_handler_class() is LiteLLMAIHandler

    def test_explicit_setting_overrides_model_inference(self):
        pytest.importorskip("litellm", reason="litellm is a hard dependency; skipped only where it fails to build")
        from pr_agent.algo.ai_handlers.litellm_ai_handler import \
            LiteLLMAIHandler
        get_settings().set("CONFIG.AI_HANDLER", "litellm")
        get_settings().set("CONFIG.MODEL", "cli/claude/opus")
        assert get_ai_handler_class() is LiteLLMAIHandler

    def test_unknown_handler_raises(self):
        get_settings().set("CONFIG.AI_HANDLER", "nope")
        with pytest.raises(ValueError, match="Unknown config.ai_handler"):
            get_ai_handler_class()


class TestChatCompletion:
    @pytest.mark.asyncio
    async def test_claude_roundtrip(self):
        payload = json.dumps({"type": "result", "subtype": "success",
                              "result": "code_suggestions: []",
                              "usage": {"input_tokens": 1, "output_tokens": 2}})
        handler = CliAgentAIHandler()
        with patch("pr_agent.algo.ai_handlers.cli_agent_ai_handler._resolve_binary",
                   return_value="/usr/bin/claude"), \
             patch.object(CliAgentAIHandler, "_exec", return_value=(payload, "")) as exec_mock:
            resp, finish_reason = await handler.chat_completion(
                model="cli/claude/opus", system="SYS", user="USR")
        assert (resp, finish_reason) == ("code_suggestions: []", "stop")
        # The prompt must go over stdin, never argv: diffs blow past the argv size limit.
        assert exec_mock.call_args.kwargs["stdin_text"] == "USR"

    @pytest.mark.asyncio
    async def test_codex_flattens_system_into_prompt(self):
        handler = CliAgentAIHandler()
        with patch("pr_agent.algo.ai_handlers.cli_agent_ai_handler._resolve_binary",
                   return_value="/usr/bin/codex"), \
             patch.object(CliAgentAIHandler, "_exec", return_value=("transcript", "")) as exec_mock, \
             patch("builtins.open", create=True) as open_mock:
            open_mock.return_value.__enter__.return_value.read.return_value = "answer"
            resp, finish_reason = await handler.chat_completion(
                model="cli/codex/gpt-5", system="SYS", user="USR")
        assert (resp, finish_reason) == ("answer", "stop")
        prompt = exec_mock.call_args.kwargs["stdin_text"]
        assert prompt.startswith("SYS") and prompt.endswith("USR")

    @pytest.mark.asyncio
    async def test_empty_response_raises(self):
        payload = json.dumps({"type": "result", "subtype": "success", "result": "  "})
        handler = CliAgentAIHandler()
        with patch("pr_agent.algo.ai_handlers.cli_agent_ai_handler._resolve_binary",
                   return_value="/usr/bin/claude"), \
             patch.object(CliAgentAIHandler, "_exec", return_value=(payload, "")):
            with pytest.raises(CliAgentError, match="empty response"):
                await handler.chat_completion(model="cli/claude", system="S", user="U")

    @pytest.mark.asyncio
    async def test_missing_binary_is_not_retried(self):
        handler = CliAgentAIHandler()
        with patch("pr_agent.algo.ai_handlers.cli_agent_ai_handler.shutil.which",
                   return_value=None) as which_mock:
            with pytest.raises(CliAgentConfigError, match="not found on PATH"):
                await handler.chat_completion(model="cli/claude", system="S", user="U")
        assert which_mock.call_count == 1, "config errors must not burn retry attempts"
