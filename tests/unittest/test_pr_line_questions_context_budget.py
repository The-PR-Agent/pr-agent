"""Regression tests for the /ask_line conversation-context budget."""

from types import SimpleNamespace

import pytest
from litellm import token_counter

import pr_agent.tools.pr_line_questions as plq
from pr_agent.algo.token_budget import MESSAGE_FRAMING_TOKEN_ALLOWANCE, REPLY_FRAMING_TOKEN_ALLOWANCE
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


class _FakeGithubProvider:
    def __init__(self, comments, patch):
        self.comments = comments
        self.patch = patch
        self.replies = []

    def get_review_thread_comments(self, comment_id):
        return self.comments

    def supports_line_question_history(self):
        return True

    def supports_threaded_pr_questions(self):
        return False

    def reply_to_comment_from_comment_id(self, comment_id, body):
        self.replies.append((comment_id, body))


class _RecordingAIHandler:
    def __init__(self, fail_models=()):
        self.requests = []
        self.fail_models = set(fail_models)

    async def chat_completion(self, *, model, temperature, system, user):
        self.requests.append({"model": model, "system": system, "user": user})
        if model in self.fail_models:
            raise TimeoutError(f"simulated failure for {model}")
        return "answer", "stop"


@pytest.mark.parametrize(
    ("fallback_models", "fail_models", "expected_model"),
    [
        ([], (), "gpt-4o"),
        (["gpt-3.5-turbo"], ("gpt-4o",), "gpt-3.5-turbo"),
    ],
)
@pytest.mark.asyncio
async def test_ask_line_keeps_final_prompt_within_budget_after_loading_history(
    monkeypatch, fallback_models, fail_models, expected_model
):
    settings = get_settings()
    keys = (
        "config.model",
        "config.model_weak",
        "config.fallback_models",
        "config.max_model_tokens",
        "config.max_output_tokens",
        "openai.deployment_id",
        "openai.fallback_deployments",
        "pr_questions.use_conversation_history",
        "ask_diff_hunk",
        "line_start",
        "line_end",
        "side",
        "file_name",
        "comment_id",
    )
    saved = snapshot_settings(keys)

    patch = (
        "@@ -5,7 +5,8 @@ def main():\n"
        "     a = 1\n"
        "     b = 2\n"
        "+    c = 3\n"
        "     return a\n"
    )
    comments = [
        SimpleNamespace(
            id=100,
            body="current question",
            user=SimpleNamespace(login="alice"),
        )
    ] + [
        SimpleNamespace(
            id=101 + index,
            body=f"reply {index}: " + ("context " * 20),
            user=SimpleNamespace(login="reviewer"),
        )
        for index in range(200)
    ]
    provider = _FakeGithubProvider(comments, patch)
    ai_handler = _RecordingAIHandler(fail_models)
    question = plq.PR_LineQuestions.__new__(plq.PR_LineQuestions)
    question.question_str = "Why is this change needed?"
    question.git_provider = provider
    question.ai_handler = ai_handler
    question.resolve_threads = False
    question.vars = {
        "title": "Budget regression",
        "branch": "feature/budget",
        "question": question.question_str,
        "full_hunk": "",
        "selected_lines": "",
        "conversation_history": "",
        "resolve_threads": False,
        "is_azure_devops": False,
        "extra_instructions": "",
    }

    try:
        settings.set("config.model", "gpt-4o")
        settings.set("config.model_weak", "")
        settings.set("config.fallback_models", fallback_models)
        settings.set("config.max_model_tokens", 700)
        settings.set("config.max_output_tokens", 100)
        settings.set("openai.deployment_id", None)
        settings.set("openai.fallback_deployments", [])
        settings.set("pr_questions.use_conversation_history", True)
        settings.set("ask_diff_hunk", patch)
        settings.set("line_start", 6)
        settings.set("line_end", 8)
        settings.set("side", "RIGHT")
        settings.set("file_name", "src/example.py")
        settings.set("comment_id", 100)
        await question.run()

        assert len(ai_handler.requests) == 1 + len(fail_models)
        request = next(item for item in ai_handler.requests if item["model"] == expected_model)
        prompt_tokens = token_counter(
            model=expected_model,
            messages=[
                {"role": "system", "content": request["system"]},
                {"role": "user", "content": request["user"]},
            ],
        )
        assert prompt_tokens <= 600
        assert len(request["user"]) < len("\n".join(comment.body for comment in comments))
        assert "Why is this change needed?" in request["user"]
        assert "+    c = 3" in request["user"]
        assert "reply 199" in request["user"]
        assert provider.replies == [(100, "answer")]
    finally:
        restore_settings(saved)


@pytest.mark.asyncio
async def test_ask_line_uses_attempted_model_for_non_gpt_prompt_budget(monkeypatch):
    settings = get_settings()
    keys = (
        "config.model",
        "config.model_weak",
        "config.fallback_models",
        "config.max_model_tokens",
        "config.max_output_tokens",
        "openai.deployment_id",
        "openai.fallback_deployments",
        "pr_questions.use_conversation_history",
        "ask_diff_hunk",
        "line_start",
        "line_end",
        "side",
        "file_name",
        "comment_id",
    )
    saved = snapshot_settings(keys)
    patch = "@@ -5,2 +5,3 @@ def main():\n     a = 1\n+    b = 2\n"
    comments = [
        SimpleNamespace(
            id=100,
            body="current question",
            user=SimpleNamespace(login="alice"),
        )
    ] + [
        SimpleNamespace(
            id=101 + index,
            body=f"reply {index}: " + ("context " * 20),
            user=SimpleNamespace(login="reviewer"),
        )
        for index in range(200)
    ]
    provider = _FakeGithubProvider(comments, patch)
    ai_handler = _RecordingAIHandler(("gpt-4o",))
    question = plq.PR_LineQuestions.__new__(plq.PR_LineQuestions)
    question.question_str = "Why is this change needed?"
    question.git_provider = provider
    question.ai_handler = ai_handler
    question.resolve_threads = False
    question.vars = {
        "title": "Budget regression",
        "branch": "feature/budget",
        "question": question.question_str,
        "full_hunk": "",
        "selected_lines": "",
        "conversation_history": "",
        "resolve_threads": False,
        "is_azure_devops": False,
        "extra_instructions": "",
    }

    counter_calls = []

    def model_aware_count(self, text, force_accurate=False):
        counter_calls.append(self.model)
        return len(text)

    try:
        settings.set("config.model", "gpt-4o")
        settings.set("config.model_weak", "")
        settings.set("config.fallback_models", ["claude-2"])
        settings.set("config.max_model_tokens", 3000)
        settings.set("config.max_output_tokens", 100)
        settings.set("openai.deployment_id", None)
        settings.set("openai.fallback_deployments", [])
        settings.set("pr_questions.use_conversation_history", True)
        settings.set("ask_diff_hunk", patch)
        settings.set("line_start", 6)
        settings.set("line_end", 7)
        settings.set("side", "RIGHT")
        settings.set("file_name", "src/example.py")
        settings.set("comment_id", 100)
        monkeypatch.setattr(TokenHandler, "count_tokens", model_aware_count)

        await question.run()

        request = next(item for item in ai_handler.requests if item["model"] == "claude-2")
        assert "claude-2" in counter_calls
        assert (
            len(request["system"] + request["user"])
            + 2 * MESSAGE_FRAMING_TOKEN_ALLOWANCE
            + REPLY_FRAMING_TOKEN_ALLOWANCE
            <= 2900
        )
        assert "reply 199" in request["user"]
        assert provider.replies == [(100, "answer")]
    finally:
        restore_settings(saved)


@pytest.mark.parametrize(
    ("side", "file_name", "renamed"),
    [
        ("LEFT", "old/src/app.py", True),
        ("LEFT", "new/src/app.py", True),
        ("LEFT", "src/app.py", False),
        ("RIGHT", "src/app.py", False),
    ],
)
@pytest.mark.asyncio
async def test_ask_line_matches_renamed_file_on_either_side_path(
    monkeypatch, side, file_name, renamed
):
    settings = get_settings()
    keys = (
        "config.model",
        "config.model_weak",
        "config.fallback_models",
        "config.max_model_tokens",
        "config.max_output_tokens",
        "openai.deployment_id",
        "openai.fallback_deployments",
        "pr_questions.use_conversation_history",
        "ask_diff_hunk",
        "line_start",
        "line_end",
        "side",
        "file_name",
        "comment_id",
    )
    saved = snapshot_settings(keys)
    patch = (
        "@@ -9,5 +9,5 @@ def renamed():\n"
        "     kept_a\n"
        "     kept_b\n"
        "     kept_c\n"
        "     kept_d\n"
    )

    class _ProviderWithDiff:
        def __init__(self):
            self.replies = []

        def get_diff_files(self):
            if renamed:
                return [
                    FilePatchInfo(
                        base_file="",
                        head_file="",
                        patch=patch,
                        filename="new/src/app.py",
                        edit_type=EDIT_TYPE.RENAMED,
                        old_filename="old/src/app.py",
                    )
                ]
            return [
                FilePatchInfo(
                    base_file="",
                    head_file="",
                    patch=patch,
                    filename="src/app.py",
                    edit_type=EDIT_TYPE.MODIFIED,
                    old_filename=None,
                )
            ]

        def supports_line_question_history(self):
            return False

        def supports_threaded_pr_questions(self):
            return False

        def reply_to_comment_from_comment_id(self, comment_id, body):
            self.replies.append((comment_id, body))

    provider = _ProviderWithDiff()
    ai_handler = _RecordingAIHandler()
    question = plq.PR_LineQuestions.__new__(plq.PR_LineQuestions)
    question.question_str = "Why was this file renamed?"
    question.git_provider = provider
    question.ai_handler = ai_handler
    question.resolve_threads = False
    question.vars = {
        "title": "Renamed file",
        "branch": "feature/rename",
        "question": question.question_str,
        "full_hunk": "",
        "selected_lines": "",
        "conversation_history": "",
        "resolve_threads": False,
        "is_azure_devops": False,
        "extra_instructions": "",
    }

    try:
        settings.set("config.model", "gpt-4o")
        settings.set("config.model_weak", "")
        settings.set("config.fallback_models", [])
        settings.set("config.max_model_tokens", 3000)
        settings.set("config.max_output_tokens", 100)
        settings.set("openai.deployment_id", None)
        settings.set("openai.fallback_deployments", [])
        settings.set("pr_questions.use_conversation_history", False)
        settings.set("ask_diff_hunk", "")
        settings.set("line_start", 9)
        settings.set("line_end", 10)
        settings.set("side", side)
        settings.set("file_name", file_name)
        settings.set("comment_id", 100)

        await question.run()

        assert ai_handler.requests, f"expected the model to be called for {side}/{file_name}"
        assert provider.replies == [(100, "answer")]
    finally:
        restore_settings(saved)
