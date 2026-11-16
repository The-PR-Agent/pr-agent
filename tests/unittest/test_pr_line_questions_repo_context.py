"""Repository instructions for inline /ask_line questions."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from starlette_context import request_cycle_context

import pr_agent.commands.pr_line_questions as line_questions
from pr_agent.algo import token_budget
from pr_agent.commands.pr_questions import REPO_CONTEXT_OMITTED_NOTE
from pr_agent.config_loader import get_settings

PATCH = "@@ -1,2 +1,3 @@\n value = 1\n+validate(value)\n"
GUIDANCE = "Check that request handlers validate their input."


class _Provider:
    def __init__(self, guidance=GUIDANCE):
        self.pr = SimpleNamespace(title="Validate input")
        self.guidance = guidance
        self.repo_reads = []
        self.replies = []
        self.resolved = []

    def get_pr_branch(self):
        return "feature/validate"

    def get_repo_context_ref(self, from_default_branch=False):
        return None

    def get_repo_file_content(self, path, from_default_branch=False):
        self.repo_reads.append((path, from_default_branch))
        return self.guidance

    def supports_thread_resolution(self):
        return True

    def supports_threaded_pr_questions(self):
        return False

    def supports_line_question_history(self):
        return True

    def get_review_thread_comments(self, comment_id):
        return [SimpleNamespace(id=100, user=SimpleNamespace(login="reviewer"), body="Earlier discussion")]

    def reply_to_comment_from_comment_id(self, comment_id, body):
        self.replies.append(body)

    def resolve_comment_thread(self, comment_id):
        self.resolved.append(comment_id)
        return True


class _RecordingAI:
    def __init__(self, fail_first=False):
        self.requests = []
        self.fail_first = fail_first
        self.answer = "Answer based on the selected lines."

    async def chat_completion(self, *, model, temperature, system, user):
        self.requests.append({"model": model, "system": system, "user": user})
        if self.fail_first and len(self.requests) == 1:
            raise TimeoutError("primary unavailable")
        return self.answer, "stop"


@pytest.fixture
def line_settings():
    get_settings().as_dict()
    with request_cycle_context({"settings": deepcopy(get_settings())}):
        settings = get_settings()
        for key, value in {
            "config.model": "gpt-4o",
            "config.model_weak": "",
            "config.fallback_models": [],
            "config.max_model_tokens": 20_000,
            "config.max_output_tokens": 100,
            "config.output_run_details": False,
            "config.publish_output": True,
            "config.repo_context_files": ["AGENTS.md"],
            "config.repo_context_from_default_branch": True,
            "config.repo_context_max_lines": 500,
            "config.repo_context_expand_imports": False,
            "config.repo_context_sibling_repos": [],
            "skills.enabled": False,
            "pr_questions.use_conversation_history": False,
            "pr_questions.extra_instructions": "",
            "ask_diff_hunk": PATCH,
            "file_name": "src/example.py",
            "line_start": 1,
            "line_end": 2,
            "side": "RIGHT",
            "comment_id": 123,
            "openai.deployment_id": None,
            "openai.fallback_deployments": [],
        }.items():
            settings.set(key, value)
        yield settings


def _make_question(monkeypatch, guidance=GUIDANCE, fail_first=False):
    provider = _Provider(guidance)
    handler = _RecordingAI(fail_first)
    monkeypatch.setattr(line_questions, "get_git_provider_for_command", lambda _url: provider)
    tool = line_questions.PR_LineQuestions(
        "https://example.test/org/repo/pull/1", args=["Does this follow our rules?"],
        ai_handler=lambda: handler,
    )
    return tool, provider, handler


@pytest.mark.parametrize("enabled", [True, False])
async def test_line_questions_use_shared_repo_instructions_by_default(monkeypatch, line_settings, enabled):
    if not enabled:
        line_settings.set("config.repo_context_files", [])
    tool, provider, handler = _make_question(monkeypatch)

    await tool.run()

    [request] = handler.requests
    assert provider.repo_reads == ([("AGENTS.md", True)] if enabled else [])
    assert (GUIDANCE in request["system"]) is enabled
    assert "Does this follow our rules?" in request["user"]
    assert "+validate(value)" in request["user"]
    assert REPO_CONTEXT_OMITTED_NOTE not in provider.replies[0]


async def test_oversized_instructions_keep_original_prompt(monkeypatch, line_settings):
    line_settings.set("pr_questions.use_conversation_history", True)
    line_settings.set("pr_questions.resolve_threads", True)
    line_settings.set("config.repo_context_files", [])
    baseline, _, baseline_handler = _make_question(monkeypatch)
    baseline_handler.answer += " [THREAD_RESOLVED]"
    await baseline.run()

    line_settings.set("config.repo_context_files", ["AGENTS.md"])
    tool, provider, handler = _make_question(monkeypatch, "oversized guidance " * 10_000)
    handler.answer += " [THREAD_RESOLVED]"
    await tool.run()

    assert handler.requests == baseline_handler.requests
    assert "Earlier discussion" in handler.requests[0]["user"]
    assert provider.repo_reads == [("AGENTS.md", True)]
    assert provider.replies[0].count(REPO_CONTEXT_OMITTED_NOTE) == 1
    assert "[THREAD_RESOLVED]" not in provider.replies[0]
    assert provider.resolved == [123]


async def test_fallback_refits_instructions_without_another_read(monkeypatch, line_settings):
    line_settings.set("config.fallback_models", ["gpt-4o-mini"])
    monkeypatch.setitem(token_budget.MAX_TOKENS, "gpt-4o", 1_200)
    monkeypatch.setitem(token_budget.MAX_TOKENS, "gpt-4o-mini", 8_000)
    tool, provider, handler = _make_question(monkeypatch, "Repository convention. " * 350, fail_first=True)

    await tool.run()

    assert [request["model"] for request in handler.requests] == ["gpt-4o", "gpt-4o-mini"]
    assert [GUIDANCE in request["system"] for request in handler.requests] == [False, False]
    assert ["Repository convention." in request["system"] for request in handler.requests] == [False, True]
    assert provider.repo_reads == [("AGENTS.md", True)]
    assert REPO_CONTEXT_OMITTED_NOTE not in provider.replies[0]


async def test_missing_hunk_skips_instruction_reads(monkeypatch, line_settings):
    line_settings.set("ask_diff_hunk", "not a diff hunk")
    tool, provider, handler = _make_question(monkeypatch)

    await tool.run()

    assert provider.repo_reads == []
    assert handler.requests == []
    assert "Could not find the requested lines" in provider.replies[0]


async def test_repo_instruction_fetch_failure_keeps_inline_answer(monkeypatch, line_settings):
    tool, provider, handler = _make_question(monkeypatch)

    def unavailable(path, from_default_branch=False):
        provider.repo_reads.append((path, from_default_branch))
        raise OSError("temporary repository API failure")

    monkeypatch.setattr(provider, "get_repo_file_content", unavailable)
    await tool.run()

    assert provider.repo_reads == [("AGENTS.md", True)]
    assert len(handler.requests) == 1
    assert GUIDANCE not in handler.requests[0]["system"]
    assert provider.replies[0].startswith(handler.answer)
