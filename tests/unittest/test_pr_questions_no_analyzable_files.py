"""/ask when the PR or its requested file has no usable diff."""

from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from github.PullRequestComment import PullRequestComment
from httpx import Request
from openai import APIError
from starlette_context import request_cycle_context

from pr_agent.algo.run_details import command_failed, init_run_details
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.tools.pr_questions import (
    FILE_QUESTION_BUDGET_NOTE,
    NO_ANALYZABLE_FILE_NOTE,
    NO_ANALYZABLE_FILES_NOTE,
    PRQuestions,
)
from tests.unittest._run_details_command_helpers import (
    BACKUP,
    PRIMARY,
    TITLE,
    _provider,
    _published_bodies,
    command_run_settings,
)


@pytest.fixture(autouse=True)
def _run_settings():
    original_extra_instructions = get_settings().get("pr_questions.extra_instructions")
    with command_run_settings() as settings:
        settings.set("pr_questions.enable_help_text", False)
        try:
            yield settings
        finally:
            settings.set("pr_questions.extra_instructions", original_extra_instructions)


def _make_ask(monkeypatch, remaining_files):
    """Run the real _prepare_prediction against a diff with no analyzable patch left."""
    tool = PRQuestions.__new__(PRQuestions)
    tool.pr_url = "https://example.com/org/repo/pull/1"
    tool.question_str = "Is this change safe?"
    tool.vars = {"conversation_history": "", "diff": ""}
    tool.prediction = None
    tool.patches_diff = None
    tool.no_analyzable_files = False
    tool.git_provider = _provider()
    tool.ai_handler = MagicMock(spec=[])
    tool._get_prediction = AsyncMock(return_value="unused")
    requested_models = []

    def fake_get_pr_diff(_provider, _token_handler, model, return_remaining_files=False, **_kwargs):
        requested_models.append(model)
        # The diff is empty: every changed file was dropped by file-type filtering (lockfile-only
        # PR), or the files are still there but did not fit the budget.
        return ("", list(remaining_files)) if return_remaining_files else ""

    monkeypatch.setattr("pr_agent.tools.pr_questions.get_pr_diff", fake_get_pr_diff)
    monkeypatch.setattr(
        "pr_agent.tools.pr_questions.AttemptTokenBudget.for_prompt_attempt",
        lambda *_args, **_kwargs: MagicMock(),
    )
    init_run_details()
    return tool, requested_models


async def test_ask_answers_with_a_note_when_every_changed_file_is_filtered(monkeypatch):
    get_settings().set("file_name", "previous-line-question.py")
    tool, requested_models = _make_ask(monkeypatch, remaining_files=[])

    await tool.run()

    tool._get_prediction.assert_not_awaited()
    assert requested_models == [PRIMARY]
    assert command_failed() is False
    [body] = _published_bodies(tool.git_provider)
    assert "Is this change safe?" in body
    assert f"### **Answer:**\n{NO_ANALYZABLE_FILES_NOTE}" in body
    # No model answered, so there are no run details to report.
    assert TITLE not in body
    tool.git_provider.publish_comment.assert_any_call("Preparing answer...", is_temporary=True)
    tool.git_provider.remove_initial_comment.assert_called_once()


async def test_ask_stores_the_note_when_output_is_not_published(monkeypatch):
    get_settings().set("config.publish_output", False)
    tool, _ = _make_ask(monkeypatch, remaining_files=[])

    await tool.run()

    tool._get_prediction.assert_not_awaited()
    assert get_settings().get("data.answer") == NO_ANALYZABLE_FILES_NOTE
    assert _published_bodies(tool.git_provider) == []


async def test_ask_still_walks_the_fallback_chain_when_files_remain_but_do_not_fit(monkeypatch):
    tool, requested_models = _make_ask(monkeypatch, remaining_files=["app.py"])

    with pytest.raises(Exception, match="Failed to generate prediction"):
        await tool.run()

    tool._get_prediction.assert_not_awaited()
    assert requested_models == [PRIMARY, BACKUP]
    assert _published_bodies(tool.git_provider) == []
    tool.git_provider.remove_initial_comment.assert_called_once()


@pytest.fixture
def file_question(monkeypatch):
    settings = get_settings()
    settings.set("config.model", "gpt-4o")
    settings.set("config.fallback_models", [])
    settings.set("config.output_run_details", False)
    monkeypatch.setitem(settings.config, "max_model_tokens", 4500)

    provider = _provider()
    provider.pr = SimpleNamespace(title="Change target behavior")
    provider.get_pr_branch.return_value = "feature/target"
    provider.get_pr_description.return_value = ""
    provider.get_commit_messages.return_value = ""
    provider.get_languages.return_value = {"Python": 100}
    provider.get_files.return_value = ["src/other.py", "src/target.py"]
    provider.get_filtered_diff_file_names.return_value = []
    other_patch = "@@ -1,100 +1,100 @@\n" + "".join(
        f"-other_value_{index} = 1\n+other_value_{index} = 2\n" for index in range(100)
    )
    provider.get_diff_files.return_value = [FilePatchInfo(
        "", "", other_patch, "src/other.py", edit_type=EDIT_TYPE.MODIFIED,
    )]
    handler = SimpleNamespace(chat_completion=AsyncMock(return_value=("The target changed.", "stop")))
    monkeypatch.setattr("pr_agent.tools.pr_questions.get_git_provider_for_tool", lambda _url: provider)
    with request_cycle_context({
        "file_question_path": "src/target.py",
        "file_question_comment_id": 123,
        "file_question_origin_comment_id": 789,
        "file_question_comment_url": "https://github.com/org/repo/pull/1#discussion_r789",
    }):
        yield PRQuestions(
            "https://example.com/org/repo/pull/1", args=["Why did this file change?"], ai_handler=lambda: handler,
        )


def _file_answer(provider):
    provider.reply_to_comment_from_comment_id.assert_called_once()
    comment_id, body = provider.reply_to_comment_from_comment_id.call_args.args
    assert comment_id == 123
    assert "[View question](https://github.com/org/repo/pull/1#discussion_r789)" in body
    assert _published_bodies(provider) == [body]
    return body


@pytest.mark.parametrize("use_history", [True, False])
async def test_file_question_preserves_thread_history(file_question, monkeypatch, use_history):
    monkeypatch.setitem(get_settings().pr_questions, "use_conversation_history", use_history)
    provider = file_question.git_provider
    provider.supports_line_question_history.return_value = True
    question = "Does that still apply here?"
    payloads = [
        {"id": 123, "body": "Keep the nullable fallback."},
        {"id": 124, "in_reply_to_id": 123, "body": "That preserves old clients."},
        {"id": 789, "in_reply_to_id": 123, "body": f"/ask {question}"},
        {"id": 900, "body": "Unrelated thread context."},
    ]
    comments = [PullRequestComment(MagicMock(), {}, {
        **payload, "user": {"login": "reviewer"},
        "url": f"https://api.github.com/repos/org/repo/pulls/comments/{payload['id']}",
    }) for payload in payloads]
    provider.pr.get_comments = MagicMock(return_value=comments)
    provider.get_review_thread_comments = MethodType(GithubProvider.get_review_thread_comments, provider)
    provider.get_diff_files.return_value.append(FilePatchInfo(
        "", "", "@@ -33 +33 @@\n-target = 1\n+target = 2\n", "src/target.py",
    ))
    tool = PRQuestions(file_question.pr_url, args=[question], ai_handler=lambda: file_question.ai_handler)

    await tool.run()

    tool.ai_handler.chat_completion.assert_awaited_once()
    prompt = tool.ai_handler.chat_completion.await_args.kwargs["user"]
    assert ("Keep the nullable fallback." in prompt) is use_history
    assert ("That preserves old clients." in prompt) is use_history
    assert "Unrelated thread context." not in prompt
    assert prompt.count(question) == 1
    assert provider.pr.get_comments.call_count == int(use_history)


@pytest.mark.parametrize("use_history", [True, False])
async def test_file_question_keeps_the_complete_diff_when_history_is_long(file_question, monkeypatch, use_history):
    monkeypatch.setitem(get_settings().pr_questions, "use_conversation_history", use_history)
    provider = file_question.git_provider
    provider.supports_line_question_history.return_value = True
    provider.get_review_thread_comments.return_value = [SimpleNamespace(
        id=124, body="Earlier discussion. " * 2500, user=SimpleNamespace(login="reviewer"),
    )]
    patch = "@@ -0,0 +1,410 @@\n" + "+changed = 1\n" * 410
    provider.get_diff_files.return_value.append(FilePatchInfo(
        "", "", patch, "src/target.py", edit_type=EDIT_TYPE.ADDED,
    ))
    tool = PRQuestions(file_question.pr_url, args=["Explain this change"], ai_handler=lambda: file_question.ai_handler)

    await tool.run()

    tool.ai_handler.chat_completion.assert_awaited_once()
    prompt = tool.ai_handler.chat_completion.await_args.kwargs["user"]
    assert patch in prompt
    assert ("Earlier discussion." in prompt) is use_history
    assert "The target changed." in _file_answer(provider)


@pytest.mark.parametrize("use_history", [True, False])
async def test_file_question_can_fit_a_small_diff_when_history_reserve_is_tight(file_question, monkeypatch,
                                                                                use_history):
    settings = get_settings()
    monkeypatch.setitem(settings.pr_questions, "use_conversation_history", use_history)
    settings.set("pr_questions.extra_instructions", "guidance " * 2900)
    provider = file_question.git_provider
    provider.supports_line_question_history.return_value = True
    provider.get_review_thread_comments.return_value = [SimpleNamespace(
        id=124, body="Earlier discussion. " * 400, user=SimpleNamespace(login="reviewer"),
    )]
    patch = "@@ -33 +33 @@\n-target = 1\n+target = 2\n"
    provider.get_diff_files.return_value.append(FilePatchInfo("", "", patch, "src/target.py"))
    tool = PRQuestions(file_question.pr_url, args=["Explain this change"], ai_handler=lambda: file_question.ai_handler)

    await tool.run()

    tool.ai_handler.chat_completion.assert_awaited_once()
    prompt = tool.ai_handler.chat_completion.await_args.kwargs["user"]
    assert patch in prompt
    assert ("Earlier discussion." in prompt) is use_history
    assert "The target changed." in _file_answer(provider)


@pytest.mark.parametrize("edit_type", [EDIT_TYPE.MODIFIED, EDIT_TYPE.RENAMED, EDIT_TYPE.DELETED])
async def test_file_question_sends_only_the_complete_target_patch(file_question, edit_type):
    tool = file_question
    patch = (
        "@@ -1 +0,0 @@\n-target = 1\n" if edit_type == EDIT_TYPE.DELETED
        else "@@ -33 +33 @@\n-target = 1\n+target = 2\n"
    )
    tool.git_provider.get_diff_files.return_value.append(FilePatchInfo(
        "", "", patch, "src/target.py", edit_type=edit_type,
        old_filename="src/before.py" if edit_type == EDIT_TYPE.RENAMED else None,
    ))

    await tool.run()

    tool.ai_handler.chat_completion.assert_awaited_once()
    prompt = tool.ai_handler.chat_completion.await_args.kwargs["user"]
    assert patch in prompt
    assert "src/target.py" in prompt
    assert "src/other.py" not in prompt
    assert "other_value_" not in prompt
    assert "The target changed." in _file_answer(tool.git_provider)


@pytest.mark.parametrize("empty_patch", [False, True], ids=["missing", "empty-patch"])
async def test_file_question_reports_unavailable_target_without_a_model_call(file_question, empty_patch):
    tool = file_question
    if empty_patch:
        tool.git_provider.get_diff_files.return_value.append(FilePatchInfo("", "", "", "src/target.py"))

    await tool.run()

    tool.ai_handler.chat_completion.assert_not_awaited()
    body = _file_answer(tool.git_provider)
    assert f"### **Answer:**\n{NO_ANALYZABLE_FILE_NOTE}" in body
    tool.git_provider.remove_initial_comment.assert_called_once()


@pytest.mark.parametrize("publish_output", [True, False])
async def test_file_question_reports_an_oversized_target_after_all_models_fail(file_question, publish_output):
    tool = file_question
    get_settings().set("config.fallback_models", ["gpt-4o-mini"])
    get_settings().set("config.publish_output", publish_output)
    patch = "@@ -0,0 +1,4000 @@\n" + "+target = 2\n" * 4000
    tool.git_provider.get_diff_files.return_value.append(FilePatchInfo(
        "", "", patch, "src/target.py", edit_type=EDIT_TYPE.ADDED,
    ))

    await tool.run()

    assert tool.git_provider.get_diff_files.call_count == 2
    tool.ai_handler.chat_completion.assert_not_awaited()
    if publish_output:
        body = _file_answer(tool.git_provider)
        assert f"### **Answer:**\n{FILE_QUESTION_BUDGET_NOTE}" in body
        assert TITLE not in body
        tool.git_provider.remove_initial_comment.assert_called_once()
    else:
        assert get_settings().get("data.answer") == FILE_QUESTION_BUDGET_NOTE
        assert _published_bodies(tool.git_provider) == []
        tool.git_provider.remove_initial_comment.assert_not_called()


@pytest.mark.parametrize("primary_fits", [False, True], ids=["larger-fallback", "api-error-before-smaller-fallback"])
async def test_file_question_fallback_preserves_transport_errors(file_question, monkeypatch, primary_fits):
    tool = file_question
    settings = get_settings()
    settings.set("config.model", "gpt-4o" if primary_fits else "gpt-4")
    settings.set("config.fallback_models", ["gpt-4" if primary_fits else "gpt-4o"])
    monkeypatch.setitem(settings.config, "max_model_tokens", 40000)
    patch = "@@ -0,0 +1,4000 @@\n" + "+target = 2\n" * 4000
    tool.git_provider.get_diff_files.return_value.append(FilePatchInfo(
        "", "", patch, "src/target.py", edit_type=EDIT_TYPE.ADDED,
    ))

    if primary_fits:
        tool.ai_handler.chat_completion.side_effect = APIError(
            "primary unavailable", request=Request("POST", "https://example.com/model"), body=None,
        )
        with pytest.raises(Exception, match="Failed to generate prediction"):
            await tool.run()
        assert _published_bodies(tool.git_provider) == []
    else:
        await tool.run()
        body = _file_answer(tool.git_provider)
        assert "The target changed." in body
        assert FILE_QUESTION_BUDGET_NOTE not in body

    assert tool.git_provider.get_diff_files.call_count == 2
    tool.ai_handler.chat_completion.assert_awaited_once()
    assert tool.ai_handler.chat_completion.await_args.kwargs["model"] == "gpt-4o"
    assert patch in tool.ai_handler.chat_completion.await_args.kwargs["user"]
    tool.git_provider.remove_initial_comment.assert_called_once()


async def test_file_question_reports_a_fixed_prompt_budget_failure_before_preparing_diff(file_question, monkeypatch):
    monkeypatch.setitem(get_settings().pr_questions, "extra_instructions", "guidance " * 5000)
    tool = PRQuestions(
        file_question.pr_url, args=["Why did this file change?"], ai_handler=lambda: file_question.ai_handler,
    )
    tool.git_provider.get_diff_files.return_value.append(FilePatchInfo(
        "", "", "@@ -33 +33 @@\n-target = 1\n+target = 2\n", "src/target.py",
    ))

    await tool.run()

    tool.git_provider.get_diff_files.assert_not_called()
    tool.ai_handler.chat_completion.assert_not_awaited()
    assert FILE_QUESTION_BUDGET_NOTE in _file_answer(tool.git_provider)
    tool.git_provider.remove_initial_comment.assert_called_once()


async def test_file_question_keeps_the_budget_note_when_the_question_reaches_the_comment_limit(file_question):
    provider = file_question.git_provider
    request = MagicMock(return_value=({}, {}))
    provider.pr._requester = SimpleNamespace(requestJsonAndCheck=request)
    provider.base_url = "https://api.github.com"
    provider.repo = "org/repo"
    provider.pr_num = 1
    provider.max_comment_chars = 65000
    provider.reply_to_comment_from_comment_id = MethodType(GithubProvider.reply_to_comment_from_comment_id, provider)
    provider.limit_output_characters = MethodType(GithubProvider.limit_output_characters, provider)
    question = "Why " * 16250
    tool = PRQuestions(file_question.pr_url, args=[question], ai_handler=lambda: file_question.ai_handler)

    await tool.run()

    tool.ai_handler.chat_completion.assert_not_awaited()
    request.assert_called_once()
    assert request.call_args.args == (
        "POST", "https://api.github.com/repos/org/repo/pulls/1/comments/123/replies",
    )
    body = request.call_args.kwargs["input"]["body"]
    assert "[View question](https://github.com/org/repo/pull/1#discussion_r789)" in body
    assert f"### **Answer:**\n{FILE_QUESTION_BUDGET_NOTE}" in body
    assert len(body) < provider.max_comment_chars
    assert question[:100] not in body
    assert "src/target.py" not in body
    assert all(call.kwargs.get("is_temporary") for call in provider.publish_comment.call_args_list)
    provider.remove_initial_comment.assert_called_once()
