import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.algo.ai_handlers.litellm_helpers import AssistantTurn, ToolCall
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_questions import PRQuestions
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_SETTINGS_KEYS = ["pr_questions.enable_tools", "pr_questions.max_tool_tokens"]


def _make_pr_questions(
    question_str: str = "Explain the change",
    diff_files: list[FilePatchInfo] = None,
    ai_handler=None,
) -> PRQuestions:
    obj = PRQuestions.__new__(PRQuestions)
    obj.question_str = question_str
    obj.prediction = ""
    obj.vars = {
        "title": "PR Title",
        "branch": "feature",
        "description": "PR description",
        "language": "python",
        "diff": "",
        "questions": question_str,
        "conversation_history": "",
        "commit_messages_str": "",
        "extra_instructions": "",
        "skills_context": "",
    }
    obj.patches_diff = "diff --git a/foo.py b/foo.py\n..."
    git_provider = MagicMock()
    git_provider.supports_threaded_pr_questions.return_value = False
    git_provider.get_diff_files.return_value = diff_files or []
    obj.git_provider = git_provider
    obj._diff_files = diff_files
    if ai_handler is None:
        ai_handler = MagicMock()
        ai_handler.supports_tool_calling = MagicMock(return_value=True)
    obj.ai_handler = ai_handler
    return obj


# ---------------------------------------------------------------------------
# Tool capability gating (_should_use_tools)
# ---------------------------------------------------------------------------


class TestShouldUseTools:
    def test_disabled_by_default(self):
        pr = _make_pr_questions()
        assert not get_settings().pr_questions.get("enable_tools", False)
        assert pr._should_use_tools("gpt-4o") is False

    def test_disabled_when_image_present(self, monkeypatch):
        snapshot = snapshot_settings(_SETTINGS_KEYS)
        try:
            get_settings().set("pr_questions.enable_tools", True)
            pr = _make_pr_questions()
            pr.vars["img_path"] = "https://example.com/image.png"
            assert pr._should_use_tools("gpt-4o") is False
        finally:
            restore_settings(snapshot)

    def test_disabled_when_handler_lacks_capability_method(self, monkeypatch):
        snapshot = snapshot_settings(_SETTINGS_KEYS)
        try:
            get_settings().set("pr_questions.enable_tools", True)
            handler = MagicMock(spec=["chat_completion"])
            pr = _make_pr_questions(ai_handler=handler)
            assert pr._should_use_tools("gpt-4o") is False
        finally:
            restore_settings(snapshot)

    def test_disabled_when_model_unsupported(self, monkeypatch):
        snapshot = snapshot_settings(_SETTINGS_KEYS)
        try:
            get_settings().set("pr_questions.enable_tools", True)
            handler = MagicMock()
            handler.supports_tool_calling.return_value = False
            pr = _make_pr_questions(ai_handler=handler)
            assert pr._should_use_tools("unsupported-model") is False
        finally:
            restore_settings(snapshot)

    def test_enabled_when_supported_and_configured(self, monkeypatch):
        snapshot = snapshot_settings(_SETTINGS_KEYS)
        try:
            get_settings().set("pr_questions.enable_tools", True)
            handler = MagicMock()
            handler.supports_tool_calling.return_value = True
            handler.chat_completion_with_tools = AsyncMock()
            pr = _make_pr_questions(ai_handler=handler)
            assert pr._should_use_tools("gpt-4o") is True
        finally:
            restore_settings(snapshot)


# ---------------------------------------------------------------------------
# Tool argument & path validation (_execute_read_pr_file)
# ---------------------------------------------------------------------------


class TestExecuteReadPrFileValidation:
    @pytest.fixture(autouse=True)
    def setup_diff_file(self):
        self.file_foo = FilePatchInfo(
            base_file="old foo content",
            head_file="new foo content",
            patch="",
            filename="foo.py",
            edit_type=EDIT_TYPE.MODIFIED,
            head_file_is_complete=True,
        )
        self.pr = _make_pr_questions(diff_files=[self.file_foo])

    def test_malformed_json_argument(self):
        res = json.loads(self.pr._execute_read_pr_file("not json {"))
        assert "error" in res
        assert "Malformed tool arguments" in res["error"]

    def test_non_dict_argument(self):
        res = json.loads(self.pr._execute_read_pr_file('["foo.py"]'))
        assert "error" in res
        assert "must be a JSON object" in res["error"]

    def test_unexpected_arguments_rejected(self):
        res = json.loads(self.pr._execute_read_pr_file('{"path": "foo.py", "extra": 123}'))
        assert "error" in res
        assert "Unexpected arguments" in res["error"]

    def test_missing_or_empty_path(self):
        res1 = json.loads(self.pr._execute_read_pr_file('{}'))
        assert "error" in res1
        assert "Missing or empty 'path'" in res1["error"]

        res2 = json.loads(self.pr._execute_read_pr_file('{"path": "   "}'))
        assert "error" in res2
        assert "Missing or empty 'path'" in res2["error"]

    def test_path_traversal_rejected(self):
        for bad_path in ["../secret.py", "dir/../../secret.py", "a/b/../.."]:
            res = json.loads(self.pr._execute_read_pr_file(json.dumps({"path": bad_path})))
            assert "error" in res
            assert "directory traversal" in res["error"]

    def test_absolute_path_rejected(self):
        res = json.loads(self.pr._execute_read_pr_file('{"path": "/etc/passwd"}'))
        assert "error" in res
        assert "absolute paths are not allowed" in res["error"]

    def test_backslash_rejected(self):
        res = json.loads(self.pr._execute_read_pr_file(r'{"path": "foo\\bar.py"}'))
        assert "error" in res
        assert "forward slashes" in res["error"]

    def test_null_byte_rejected(self):
        res = json.loads(self.pr._execute_read_pr_file(json.dumps({"path": "foo\x00bar.py"})))
        assert "error" in res
        assert "null byte" in res["error"]


# ---------------------------------------------------------------------------
# Tool repository matching & content retrieval
# ---------------------------------------------------------------------------


class TestExecuteReadPrFileContent:
    def test_file_not_in_pr_diff(self):
        file_foo = FilePatchInfo(
            base_file="", head_file="content", patch="", filename="foo.py",
        )
        pr = _make_pr_questions(diff_files=[file_foo])
        res = json.loads(pr._execute_read_pr_file('{"path": "untouched.py"}'))
        assert "error" in res
        assert "was not modified in this pull request" in res["error"]

    def test_deleted_file_returns_deleted_error(self):
        deleted_file = FilePatchInfo(
            base_file="old content",
            head_file="",
            patch="",
            filename="deleted.py",
            edit_type=EDIT_TYPE.DELETED,
        )
        pr = _make_pr_questions(diff_files=[deleted_file])
        res = json.loads(pr._execute_read_pr_file('{"path": "deleted.py"}'))
        assert "error" in res
        assert "was deleted in this pull request" in res["error"]

    def test_head_file_none_or_empty_returns_unavailable(self):
        file_no_head = FilePatchInfo(
            base_file="old",
            head_file=None,
            patch="",
            filename="missing_head.py",
            edit_type=EDIT_TYPE.MODIFIED,
        )
        pr = _make_pr_questions(diff_files=[file_no_head])
        res = json.loads(pr._execute_read_pr_file('{"path": "missing_head.py"}'))
        assert "error" in res
        assert "Complete head content for 'missing_head.py' is unavailable" in res["error"]

    def test_partial_head_file_returns_unavailable(self):
        file_partial = FilePatchInfo(
            base_file="old",
            head_file="partial content",
            patch="",
            filename="partial.py",
            edit_type=EDIT_TYPE.MODIFIED,
            head_file_is_complete=False,
        )
        pr = _make_pr_questions(diff_files=[file_partial])
        res = json.loads(pr._execute_read_pr_file('{"path": "partial.py"}'))
        assert "error" in res
        assert "partial content only" in res["error"]

    def test_renamed_file_serves_current_head_file(self):
        renamed_file = FilePatchInfo(
            base_file="def old_function(): pass",
            head_file="def new_function(): pass",
            patch="",
            filename="renamed_new.py",
            old_filename="renamed_old.py",
            edit_type=EDIT_TYPE.RENAMED,
            head_file_is_complete=True,
        )
        pr = _make_pr_questions(diff_files=[renamed_file])

        # Querying the current head filename succeeds with head content
        res = json.loads(pr._execute_read_pr_file('{"path": "renamed_new.py"}'))
        assert res["path"] == "renamed_new.py"
        assert res["content"] == "def new_function(): pass"
        assert "def old_function" not in res["content"]

        # Querying the old filename fails
        res_old = json.loads(pr._execute_read_pr_file('{"path": "renamed_old.py"}'))
        assert "error" in res_old
        assert "was not modified" in res_old["error"]

    def test_successful_read(self):
        file_valid = FilePatchInfo(
            base_file="v1",
            head_file="print('Hello, world!')\n",
            patch="",
            filename="src/main.py",
            edit_type=EDIT_TYPE.MODIFIED,
            head_file_is_complete=True,
        )
        pr = _make_pr_questions(diff_files=[file_valid])
        res = json.loads(pr._execute_read_pr_file('{"path": "src/main.py"}'))
        assert res["path"] == "src/main.py"
        assert res["content"] == "print('Hello, world!')\n"
        assert "truncated" not in res

    def test_large_file_truncated_with_metadata(self, monkeypatch):
        snapshot = snapshot_settings(_SETTINGS_KEYS)
        try:
            get_settings().set("pr_questions.max_tool_tokens", 10)
            large_content = "word " * 1000
            file_large = FilePatchInfo(
                base_file="",
                head_file=large_content,
                patch="",
                filename="large.txt",
                edit_type=EDIT_TYPE.ADDED,
                head_file_is_complete=True,
            )
            pr = _make_pr_questions(diff_files=[file_large])
            res = json.loads(pr._execute_read_pr_file('{"path": "large.txt"}'))
            assert res["path"] == "large.txt"
            assert res["truncated"] is True
            assert res["max_tokens"] == 10
            assert len(res["content"]) < len(large_content)
            assert "...(truncated)" in res["content"]
        finally:
            restore_settings(snapshot)


# ---------------------------------------------------------------------------
# Orchestration (_get_prediction_with_tools & _get_prediction)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOrchestration:
    async def test_turn1_returns_text_directly(self):
        pr = _make_pr_questions()
        pr.ai_handler.chat_completion_with_tools = AsyncMock(
            return_value=AssistantTurn(content="Direct answer", finish_reason="stop")
        )

        ans = await pr._get_prediction_with_tools("gpt-4o", "system", "user")
        assert ans == "Direct answer"
        assert pr.ai_handler.chat_completion_with_tools.call_count == 1

    async def test_turn1_multiple_tool_calls_triggers_text_fallback(self):
        pr = _make_pr_questions()
        # Model returns 2 tool calls, exceeding limit of 1
        turn1 = AssistantTurn(
            content=None,
            tool_calls=[
                ToolCall(id="c1", name="read_pr_file", arguments='{"path": "a.py"}'),
                ToolCall(id="c2", name="read_pr_file", arguments='{"path": "b.py"}'),
            ],
        )
        pr.ai_handler.chat_completion_with_tools = AsyncMock(return_value=turn1)
        pr.ai_handler.chat_completion = AsyncMock(return_value=("Fallback text", "stop"))

        ans = await pr._get_prediction_with_tools("gpt-4o", "system", "user")
        assert ans == "Fallback text"
        # Legacy chat_completion was called because turn1 had no text content
        pr.ai_handler.chat_completion.assert_called_once()

    async def test_successful_tool_round_to_turn2(self):
        file_target = FilePatchInfo(
            base_file="",
            head_file="def target(): return 42\n",
            patch="",
            filename="target.py",
            edit_type=EDIT_TYPE.ADDED,
            head_file_is_complete=True,
        )
        pr = _make_pr_questions(diff_files=[file_target])

        turn1 = AssistantTurn(
            content=None,
            tool_calls=[
                ToolCall(id="call_99", name="read_pr_file", arguments='{"path": "target.py"}')
            ],
            finish_reason="tool_calls",
        )
        turn2 = AssistantTurn(
            content="The target function returns 42.",
            finish_reason="stop",
        )

        pr.ai_handler.chat_completion_with_tools = AsyncMock(side_effect=[turn1, turn2])

        ans = await pr._get_prediction_with_tools("gpt-4o", "sys_prompt", "usr_prompt")
        assert ans == "The target function returns 42."
        assert pr.ai_handler.chat_completion_with_tools.call_count == 2

        # Verify second turn received assistant message and tool message
        turn2_call = pr.ai_handler.chat_completion_with_tools.call_args_list[1]
        messages = turn2_call.kwargs["messages"]
        assert len(messages) == 4
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[2]["role"] == "assistant"
        assert messages[2]["tool_calls"][0]["id"] == "call_99"
        assert messages[3]["role"] == "tool"
        assert messages[3]["tool_call_id"] == "call_99"
        tool_content = json.loads(messages[3]["content"])
        assert tool_content["path"] == "target.py"
        assert "target(): return 42" in tool_content["content"]

    async def test_turn2_requesting_additional_tools_terminates(self):
        file_target = FilePatchInfo(
            base_file="",
            head_file="content",
            patch="",
            filename="file.py",
            edit_type=EDIT_TYPE.ADDED,
            head_file_is_complete=True,
        )
        pr = _make_pr_questions(diff_files=[file_target])

        turn1 = AssistantTurn(
            content=None,
            tool_calls=[ToolCall(id="c1", name="read_pr_file", arguments='{"path": "file.py"}')],
        )
        turn2 = AssistantTurn(
            content="Partial conclusion.",
            tool_calls=[ToolCall(id="c2", name="read_pr_file", arguments='{"path": "other.py"}')],
        )
        pr.ai_handler.chat_completion_with_tools = AsyncMock(side_effect=[turn1, turn2])

        ans = await pr._get_prediction_with_tools("gpt-4o", "sys", "usr")
        # Turn 2 returned text, and tool execution did not continue
        assert ans == "Partial conclusion."
        assert pr.ai_handler.chat_completion_with_tools.call_count == 2

    async def test_image_in_vars_bypasses_tools(self, monkeypatch):
        snapshot = snapshot_settings(_SETTINGS_KEYS)
        try:
            get_settings().set("pr_questions.enable_tools", True)
            pr = _make_pr_questions()
            pr.vars["img_path"] = "https://example.com/foo.png"
            pr.ai_handler.chat_completion = AsyncMock(return_value=("Image answer", "stop"))
            pr.ai_handler.chat_completion_with_tools = AsyncMock()

            ans = await pr._get_prediction("gpt-4o")
            assert ans == "Image answer"
            pr.ai_handler.chat_completion.assert_called_once()
            pr.ai_handler.chat_completion_with_tools.assert_not_called()
        finally:
            restore_settings(snapshot)


# ---------------------------------------------------------------------------
# Regression tests: path security hardening
# ---------------------------------------------------------------------------


class TestPathSecurityRegression:
    @pytest.fixture(autouse=True)
    def setup_diff_file(self):
        self.file_foo = FilePatchInfo(
            base_file="old foo content",
            head_file="new foo content",
            patch="",
            filename="foo.py",
            edit_type=EDIT_TYPE.MODIFIED,
            head_file_is_complete=True,
        )
        self.pr = _make_pr_questions(diff_files=[self.file_foo])

    def test_path_with_raw_dotdot_in_middle_rejected(self):
        """Raw '..' segments must be rejected BEFORE normpath to prevent bypass."""
        for bad_path in [
            "valid_dir/../../../etc/passwd",
            "src/../../../secret",
            "a/b/../../c",
        ]:
            res = json.loads(self.pr._execute_read_pr_file(json.dumps({"path": bad_path})))
            assert "error" in res, f"Path '{bad_path}' should have been rejected"
            assert "directory traversal" in res["error"]

    def test_windows_drive_path_rejected(self):
        """Windows drive-qualified paths (C:/x, C:x, D:\\path) must be rejected."""
        for bad_path in ["C:/foo.py", "C:foo.py", "D:/path/to/file", "c:relative"]:
            res = json.loads(self.pr._execute_read_pr_file(json.dumps({"path": bad_path})))
            assert "error" in res, f"Path '{bad_path}' should have been rejected"
            assert "Windows drive" in res["error"] or "forward slashes" in res["error"]

    def test_empty_head_file_is_valid(self):
        """An empty string head_file is a valid empty file, not an unavailable one."""
        empty_file = FilePatchInfo(
            base_file="old content",
            head_file="",
            patch="",
            filename="empty.py",
            edit_type=EDIT_TYPE.MODIFIED,
            head_file_is_complete=True,
        )
        pr = _make_pr_questions(diff_files=[empty_file])
        res = json.loads(pr._execute_read_pr_file('{"path": "empty.py"}'))
        # Should NOT be an error — empty file is valid
        assert "error" not in res
        assert res["path"] == "empty.py"
        assert res["content"] == ""


# ---------------------------------------------------------------------------
# Regression tests: tool-call protocol validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestToolCallProtocolValidation:
    async def test_tool_call_missing_id_rejected(self):
        """A tool call with missing/empty id must be rejected and fall back."""
        pr = _make_pr_questions()
        turn1 = AssistantTurn(
            content=None,
            tool_calls=[ToolCall(id="", name="read_pr_file", arguments='{"path": "foo.py"}')],
        )
        pr.ai_handler.chat_completion_with_tools = AsyncMock(return_value=turn1)
        pr.ai_handler.chat_completion = AsyncMock(return_value=("Fallback answer", "stop"))

        ans = await pr._get_prediction_with_tools("gpt-4o", "sys", "usr")
        assert ans == "Fallback answer"
        pr.ai_handler.chat_completion.assert_called_once()

    async def test_tool_call_invalid_type_rejected(self):
        """A tool call with type != 'function' must be rejected and fall back."""
        pr = _make_pr_questions()
        turn1 = AssistantTurn(
            content=None,
            tool_calls=[ToolCall(id="c1", name="read_pr_file", arguments='{"path": "foo.py"}')],
        )
        # Override the type to something invalid
        turn1.tool_calls[0].type = "invalid_type"
        pr.ai_handler.chat_completion_with_tools = AsyncMock(return_value=turn1)
        pr.ai_handler.chat_completion = AsyncMock(return_value=("Fallback answer", "stop"))

        ans = await pr._get_prediction_with_tools("gpt-4o", "sys", "usr")
        assert ans == "Fallback answer"
        pr.ai_handler.chat_completion.assert_called_once()

    async def test_tool_call_empty_name_rejected(self):
        """A tool call with empty name must be rejected and fall back."""
        pr = _make_pr_questions()
        turn1 = AssistantTurn(
            content=None,
            tool_calls=[ToolCall(id="c1", name="", arguments='{"path": "foo.py"}')],
        )
        pr.ai_handler.chat_completion_with_tools = AsyncMock(return_value=turn1)
        pr.ai_handler.chat_completion = AsyncMock(return_value=("Fallback answer", "stop"))

        ans = await pr._get_prediction_with_tools("gpt-4o", "sys", "usr")
        assert ans == "Fallback answer"
        pr.ai_handler.chat_completion.assert_called_once()

    async def test_tool_call_malformed_arguments_returns_error_without_read(self):
        """Malformed tool arguments must produce a tool-error result returned to the
        model, but the actual read path (get_diff_files) must NOT be reached."""
        file_target = FilePatchInfo(
            base_file="",
            head_file="content",
            patch="",
            filename="target.py",
            edit_type=EDIT_TYPE.ADDED,
            head_file_is_complete=True,
        )
        pr = _make_pr_questions(diff_files=[file_target])

        turn1 = AssistantTurn(
            content=None,
            tool_calls=[
                ToolCall(id="c1", name="read_pr_file", arguments="not valid json {{{"),
            ],
            finish_reason="tool_calls",
        )
        turn2 = AssistantTurn(content="Error handled.", finish_reason="stop")
        pr.ai_handler.chat_completion_with_tools = AsyncMock(side_effect=[turn1, turn2])

        ans = await pr._get_prediction_with_tools("gpt-4o", "sys", "usr")
        assert ans == "Error handled."

        # The turn2 messages should contain a tool result with an error — verify the
        # tool result contains an error indicator and the actual read path was not reached
        turn2_call = pr.ai_handler.chat_completion_with_tools.call_args_list[1]
        messages = turn2_call.kwargs["messages"]
        tool_msg = [m for m in messages if m["role"] == "tool"]
        assert len(tool_msg) == 1
        tool_content = json.loads(tool_msg[0]["content"])
        assert "error" in tool_content
        assert "Malformed" in tool_content["error"]
        # The git_provider.get_diff_files should NOT have been called for this malformed path
        pr.git_provider.get_diff_files.assert_not_called()
