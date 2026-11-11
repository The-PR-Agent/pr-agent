"""Repository instructions fit after /ask packs its question, history and diff."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from starlette_context import request_cycle_context

import pr_agent.agent.pr_agent as agent_module
import pr_agent.commands.pr_questions as questions_module
from pr_agent.algo import token_budget
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from tests.unittest.test_pr_questions_skills import _FakeProvider

PRIMARY = "gpt-4o"
FALLBACK = "gpt-4o-mini"
REPO_GUIDANCE = "Check that request handlers validate their input."
PATCH = "@@ -1 +1,2 @@\n value = 1\n+validate(value)\n"


class _Provider(_FakeProvider):
    def __init__(self, repo_text):
        super().__init__()
        self.repo_text = repo_text
        self.repo_reads = []
        self.published = []

    def get_languages(self):
        return {"Python": 100}

    def get_diff_files(self):
        return [FilePatchInfo("value = 1\n", "value = 1\nvalidate(value)\n", PATCH,
                              "src/example.py", edit_type=EDIT_TYPE.MODIFIED)]

    def get_repo_context_ref(self, from_default_branch=False):
        return None

    def get_repo_file_content(self, file_path, from_default_branch=False):
        self.repo_reads.append((file_path, from_default_branch))
        return self.repo_text

    def supports_threaded_pr_questions(self):
        return True

    def get_review_thread_comments(self, comment_id):
        return [SimpleNamespace(id=1, user=SimpleNamespace(login="reviewer"),
                                body="Earlier discussion: " + "history context " * 1_000)]

    def is_supported(self, capability):
        return False

    def publish_comment(self, body, is_temporary=False):
        if not is_temporary:
            self.published.append(body)

    def reply_to_comment_from_comment_id(self, comment_id, body):
        self.published.append(body)

    def remove_initial_comment(self):
        pass


class _RecordingAIHandler:
    def __init__(self, fail_first=False):
        self.requests = []
        self.fail_first = fail_first

    async def chat_completion(self, *, model, temperature, system, user):
        self.requests.append({"model": model, "system": system, "user": user})
        if self.fail_first and len(self.requests) == 1:
            raise TimeoutError("first model unavailable")
        return "Answer based on the PR diff.", "stop"


@pytest.fixture
def ask_settings(monkeypatch):
    get_settings().as_dict()
    with request_cycle_context({"settings": deepcopy(get_settings())}):
        settings = get_settings()
        for key, value in {
            "config.model": PRIMARY,
            "config.model_weak": "",
            "config.fallback_models": [],
            "config.max_model_tokens": 20_000,
            "config.publish_output": False,
            "config.output_run_details": False,
            "config.output_relevant_configurations": False,
            "config.repo_context_files": ["AGENTS.md"],
            "config.repo_context_max_lines": 500,
            "config.repo_context_from_default_branch": True,
            "config.repo_context_expand_imports": False,
            "config.repo_context_sibling_repos": [],
            "openai.deployment_id": None,
            "openai.fallback_deployments": [],
            "skills.enabled": False,
            "pr_questions.enable_help_text": False,
            "pr_questions.use_conversation_history": True,
            "comment_id": 2,
        }.items():
            settings.set(key, value)
        monkeypatch.setitem(token_budget.MAX_TOKENS, PRIMARY, 4_000)
        monkeypatch.setitem(token_budget.MAX_TOKENS, FALLBACK, 12_000)
        yield settings


def _make_question(monkeypatch, repo_text=REPO_GUIDANCE, *, fail_first=False):
    provider = _Provider(repo_text)
    handler = _RecordingAIHandler(fail_first)
    monkeypatch.setattr(questions_module, "get_git_provider_for_command", lambda _url: provider)
    command = questions_module.PRQuestions(
        "https://example.test/org/repo/pull/1", args=["Does this follow our conventions?"],
        ai_handler=lambda: handler,
    )
    return command, provider, handler


@pytest.mark.parametrize("context_files", [["AGENTS.md"], []])
async def test_ask_uses_shared_repo_context_configuration(monkeypatch, ask_settings, context_files):
    ask_settings.set("config.repo_context_files", context_files)
    command, provider, handler = _make_question(monkeypatch)

    await command.run()

    [request] = handler.requests
    enabled = bool(context_files)
    assert provider.repo_reads == ([("AGENTS.md", True)] if enabled else [])
    assert (REPO_GUIDANCE in request["system"]) is enabled
    assert ("<instruction_files>" in request["system"]) is enabled
    assert "+validate(value)" in request["user"]
    assert "Does this follow our conventions?" in request["user"]
    assert questions_module.REPO_CONTEXT_OMITTED_NOTE not in ask_settings.get("data.answer")


async def test_ask_preserves_repo_instructions_ahead_of_optional_tickets(monkeypatch, ask_settings):
    ask_settings.set("pr_questions.use_conversation_history", False)
    ask_settings.set("pr_questions.use_ticket_context", True)
    repo_guidance = REPO_GUIDANCE + "\n" + ("Repository convention: validate inputs.\n" * 230)
    tickets = [
        {"ticket_url": f"https://example.test/tickets/{i}", "title": f"Ticket {i}",
         "body": "Requirement details. " * repeats}
        for i, repeats in enumerate((200, 500, 1000), start=1)
    ]

    async def load_tickets(_provider, variables, *, enabled, use_cache):
        assert enabled is True and use_cache is False
        variables["related_tickets"] = tickets

    monkeypatch.setattr(questions_module, "extract_and_cache_pr_tickets", load_tickets)
    command, provider, handler = _make_question(monkeypatch, repo_guidance)

    await command.run()

    [request] = handler.requests
    assert provider.repo_reads == [("AGENTS.md", True)]
    assert repo_guidance in request["system"]
    assert "https://example.test/tickets/1" in request["user"]
    assert "https://example.test/tickets/2" not in request["user"]
    assert "2 additional related ticket(s) were omitted" in request["user"]
    assert "+validate(value)" in request["user"]
    assert questions_module.REPO_CONTEXT_OMITTED_NOTE not in ask_settings.get("data.answer")


@pytest.mark.parametrize("publish_output", [False, True])
async def test_oversized_repo_context_preserves_diff_and_history_and_reports_omission(
    monkeypatch, ask_settings, publish_output,
):
    ask_settings.set("config.repo_context_files", [])
    baseline, _, baseline_handler = _make_question(monkeypatch)
    await baseline.run()
    ask_settings.set("config.repo_context_files", ["AGENTS.md"])
    ask_settings.set("config.publish_output", publish_output)
    # A single long line bypasses line-count trimming but must not displace the PR input.
    command, provider, handler = _make_question(monkeypatch, "repository guidance " * 10_000)

    await command.run()

    [request] = handler.requests
    assert request == baseline_handler.requests[0]
    assert "+validate(value)" in request["user"]
    assert "history context" in request["user"]
    assert provider.repo_reads == [("AGENTS.md", True)]
    answer = provider.published[0] if publish_output else ask_settings.get("data.answer")
    assert "Answer based on the PR diff." in answer
    assert answer.count(questions_module.REPO_CONTEXT_OMITTED_NOTE) == 1


@pytest.mark.parametrize("small_first,empty_context", [(True, False), (False, False), (True, True)])
async def test_fallback_refits_repo_context_without_rereading_or_retaining_omission(
    monkeypatch, ask_settings, small_first, empty_context,
):
    ask_settings.set("config.fallback_models", [FALLBACK])
    if not small_first:
        monkeypatch.setitem(token_budget.MAX_TOKENS, PRIMARY, 12_000)
        monkeypatch.setitem(token_budget.MAX_TOKENS, FALLBACK, 4_000)
    repo_text = "" if empty_context else "repository guidance " * 1_800
    command, provider, handler = _make_question(monkeypatch, repo_text, fail_first=True)

    await command.run()

    assert [request["model"] for request in handler.requests] == [PRIMARY, FALLBACK]
    assert ["<instruction_files>" in request["system"] for request in handler.requests] == [
        not small_first and not empty_context, small_first and not empty_context,
    ]
    assert all("+validate(value)" in request["user"] for request in handler.requests)
    assert provider.repo_reads == [("AGENTS.md", True)]
    answer = ask_settings.get("data.answer")
    assert (questions_module.REPO_CONTEXT_OMITTED_NOTE in answer) is (not small_first and not empty_context)


async def test_ask_command_uses_repo_context_by_default(monkeypatch, ask_settings):
    _, provider, handler = _make_question(monkeypatch)
    monkeypatch.setattr(agent_module, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(agent_module, "enforce_request_policy", lambda _url: True)
    monkeypatch.setattr(agent_module, "flush_telemetry", lambda: None)

    result = await agent_module.PRAgent(ai_handler=lambda: handler).handle_request(
        "https://example.test/org/repo/pull/1",
        '/ask "Does this follow our conventions?"',
    )

    assert result is True
    assert provider.repo_reads == [("AGENTS.md", True)]
    assert REPO_GUIDANCE in handler.requests[0]["system"]


async def test_no_analyzable_diff_does_not_read_repo_context(monkeypatch, ask_settings):
    command, provider, handler = _make_question(monkeypatch)
    monkeypatch.setattr(provider, "get_diff_files", lambda: [])

    await command.run()

    assert provider.repo_reads == []
    assert handler.requests == []
    assert ask_settings.get("data.answer") == questions_module.NO_ANALYZABLE_FILES_NOTE
