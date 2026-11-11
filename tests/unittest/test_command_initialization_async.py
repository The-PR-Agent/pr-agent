import asyncio
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from starlette_context import request_cycle_context

from pr_agent.agent import pr_agent as agent
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers import _GIT_PROVIDERS

URL = "https://example.test/owner/repo/pull/1"


def _settings():
    settings = copy.deepcopy(get_settings(use_context=False))
    settings.set("CONFIG.GIT_PROVIDER", "initialization-test")
    settings.set("CONFIG.RESPONSE_LANGUAGE", "en-us")
    settings.set("CONFIG.REPO_CONTEXT_FILES", [])
    settings.set("CONFIG.PUBLISH_OUTPUT", False)
    settings.set("CONFIG.PROPAGATE_COMMAND_ERRORS", False)
    settings.set("SKILLS.ENABLED", False)
    return settings


@pytest.fixture
def providers(monkeypatch):
    instances = {}

    def get_provider(url):
        if url not in instances:
            provider = Mock(pr=SimpleNamespace(title=url), pr_url=url)
            provider.get_repo_settings.return_value = ""
            provider.get_languages.return_value = {"Python": 1}
            provider.get_files.return_value = []
            provider.get_num_of_files.return_value = 0
            provider.get_pr_id.return_value = url
            provider.get_pr_branch.return_value = "feature"
            provider.get_commit_messages.return_value = ""
            provider.get_user_description.return_value = "Description"
            provider.get_pr_description.side_effect = lambda **kwargs: (
                ("Description", []) if kwargs.get("split_changes_walkthrough") else "Description"
            )
            provider.get_issue_comments_newest_first.return_value = []
            provider.supports_review_finding_state.return_value = False
            provider.supports_code_suggestion_state.return_value = False
            provider.supports_threaded_pr_questions.return_value = False
            provider.is_supported.side_effect = lambda capability: capability == "get_issue_comments"
            instances[url] = provider
        return instances[url]

    monkeypatch.setitem(_GIT_PROVIDERS, "initialization-test", get_provider)
    return get_provider


@pytest.mark.parametrize(("command", "command_class", "section"), [
    ("review", agent.PRReviewer, "PR_REVIEWER"),
    ("describe", agent.PRDescription, "PR_DESCRIPTION"),
    ("improve", agent.PRCodeSuggestions, "PR_CODE_SUGGESTIONS"),
    ("ask", agent.command2class["ask"], "PR_QUESTIONS"),
    ("ask_question", agent.command2class["ask_question"], "PR_QUESTIONS"),
])
async def test_real_initializers_overlap_without_mixing_request_settings(monkeypatch, providers,
                                                                        command, command_class, section):
    loop_thread = threading.get_ident()
    barrier = threading.Barrier(2, timeout=5)
    observed = []

    async def run(command):
        assert threading.get_ident() == loop_thread
        assert command.vars["title"] == command.git_provider.pr_url
        assert command.vars["extra_instructions"].startswith(command.git_provider.pr_url)
        assert "fr-fr" in command.vars["extra_instructions"]

    monkeypatch.setattr(command_class, "run", run)

    async def request(url):
        settings = _settings()
        provider = providers(url)
        provider.get_repo_settings.return_value = f'[{section}]\nextra_instructions = "{url}"\n'

        def languages():
            assert threading.get_ident() != loop_thread
            observed.append(get_settings() is settings)
            barrier.wait()
            return {"Python": 1}

        provider.get_languages.side_effect = languages
        with request_cycle_context({"settings": settings}):
            assert await agent.PRAgent().handle_request(
                url, f"/{command} --config.response_language=fr-fr"
            ) is True
            assert settings.config.response_language == "en-us"
            assert settings.get(section).extra_instructions == url

    await asyncio.gather(request(URL), request(URL + "2"))
    assert observed == [True, True]


async def test_constructor_failure_allows_a_later_review_to_retry(providers):
    provider = providers(URL)
    provider.get_languages.side_effect = [TimeoutError("temporary provider failure"), {"Python": 1}]
    with request_cycle_context({"settings": _settings()}):
        handler = agent.PRAgent()
        assert await handler.handle_request(URL, "/review --config.response_language=fr-fr") is False
        assert get_settings().config.response_language == "en-us"
        # Keep the real no-files run path; the recovered command needs no model call.
        assert await handler.handle_request(URL, "/review") is True
    assert provider.get_languages.call_count == 2


@pytest.mark.parametrize("waiter_kind", ["default", "custom_factory", "cancelled"])
def test_same_pr_waiters_leave_workers_available_for_other_requests(providers, waiter_kind):
    entered, release = threading.Event(), threading.Event()

    def languages():
        entered.set()
        assert release.wait(5)
        return {"Python": 1}

    provider = providers(URL)
    provider.get_languages.side_effect = languages

    async def scenario():
        loop = asyncio.get_running_loop()
        # Own this loop and executor so the test cannot affect another async test.
        loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
        factory_calls = []

        def factory():
            factory_calls.append(asyncio.get_running_loop())
            return SimpleNamespace()

        async def request(url, handler):
            with request_cycle_context({"settings": _settings()}):
                return await handler.handle_request(url, "/review")

        first = asyncio.create_task(request(URL, agent.PRAgent()))
        waiter = None
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            handler = agent.PRAgent(ai_handler=factory) if waiter_kind == "custom_factory" else agent.PRAgent()
            waiter = asyncio.create_task(request(URL, handler))
            await asyncio.sleep(0)
            assert not waiter.done()
            assert provider.get_languages.call_count == 1
            assert factory_calls == []
            # One worker is blocked above. The same-PR waiter must not consume the other.
            assert await asyncio.wait_for(request(URL + "2", agent.PRAgent()), 5) is True
            if waiter_kind == "cancelled":
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
                assert not first.done()
            release.set()
            assert await first is True
            if waiter_kind != "cancelled":
                assert await waiter is True
            assert provider.get_languages.call_count == (1 if waiter_kind == "cancelled" else 2)
            assert factory_calls == ([loop] if waiter_kind == "custom_factory" else [])
            assert agent._command_initialization_locks == {}
        finally:
            release.set()
            await asyncio.gather(first, *([waiter] if waiter is not None else []), return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("worker_fails", [False, True])
async def test_cancellation_waits_for_initialization_before_restoring_settings(monkeypatch, providers, worker_fails):
    entered, release = threading.Event(), threading.Event()
    observed = []

    def languages():
        entered.set()
        assert release.wait(5)
        observed.append(get_settings().config.response_language)
        if worker_fails:
            raise TimeoutError("provider failed after cancellation")
        return {"Python": 1}

    providers(URL).get_languages.side_effect = languages
    run = AsyncMock()
    monkeypatch.setattr(agent.PRReviewer, "run", run)
    with request_cycle_context({"settings": _settings()}):
        original_instructions = get_settings().pr_reviewer.extra_instructions
        task = asyncio.create_task(agent.PRAgent().handle_request(
            URL, "/review --config.response_language=fr-fr", propagate_command_errors=True
        ))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                assert get_settings().config.response_language == "fr-fr"
                assert get_settings().get("CONFIG.PROPAGATE_COMMAND_ERRORS") is True
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert observed == ["fr-fr"]
            assert get_settings().config.response_language == "en-us"
            assert get_settings().get("CONFIG.PROPAGATE_COMMAND_ERRORS") is False
            assert get_settings().pr_reviewer.extra_instructions == original_instructions
            run.assert_not_awaited()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("command", ["review", "ask"])
async def test_injected_ai_factory_keeps_its_running_loop(monkeypatch, providers, command):
    loop = asyncio.get_running_loop()
    calls = []

    def factory():
        calls.append(asyncio.get_running_loop())
        return SimpleNamespace()

    if command == "ask":
        monkeypatch.setattr(agent.command2class["ask"], "run", AsyncMock(return_value=""))
    with request_cycle_context({"settings": _settings()}):
        assert await agent.PRAgent(ai_handler=factory).handle_request(URL, f"/{command}") is True
    assert calls == [loop]


@pytest.mark.parametrize("context_kind", ["absent", "shared", "other_command"])
async def test_shared_settings_and_other_commands_keep_initialization_on_the_loop(monkeypatch, context_kind):
    loop = asyncio.get_running_loop()

    class LoopBoundCommand:
        def __init__(self, *args, **kwargs):
            assert asyncio.get_running_loop() is loop

    if context_kind != "other_command":
        monkeypatch.setattr(agent, "PRReviewer", LoopBoundCommand)
    if context_kind != "absent":
        settings = global_settings if context_kind == "shared" else _settings()
        with request_cycle_context({"settings": settings}):
            assert isinstance(await agent.PRAgent()._create_command(LoopBoundCommand, URL), LoopBoundCommand)
    else:
        assert isinstance(await agent.PRAgent()._create_command(LoopBoundCommand, URL), LoopBoundCommand)
