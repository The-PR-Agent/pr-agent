"""Verify manual commands keep diff reads off the event loop."""

import asyncio
import copy
import threading
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette_context import request_cycle_context

from pr_agent.algo.token_handler import TokenEncoder
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.tools import pr_add_docs, pr_generate_labels, pr_questions, pr_update_changelog


@pytest.mark.parametrize("module,tool_class,response", [
    (pr_questions, pr_questions.PRQuestions, "The value changes from 1 to 2."),
    (pr_generate_labels, pr_generate_labels.PRGenerateLabels, "labels: [enhancement]"),
    (pr_update_changelog, pr_update_changelog.PRUpdateChangelog, "- Update the value."),
    (pr_add_docs, pr_add_docs.PRAddDocs, "Code Documentation: []"),
], ids=["ask", "generate_labels", "update_changelog", "add_docs"])
@pytest.mark.parametrize("has_files,scoped", [(True, True), (False, True), (True, False)],
                         ids=["one-file", "empty-diff", "contextless"])
async def test_run_prepares_diff_with_request_settings(monkeypatch, module, tool_class, response, has_files, scoped):
    # Keep the actual prompt, budget, diff packing, fallback, and run paths. Only the
    # tokenizer and external provider/model boundaries are controlled by the test.
    get_settings().get("config.model")
    settings = copy.deepcopy(get_settings())
    for key, value in {
        "config.model": "gpt-4o", "config.model_weak": "gpt-4o", "config.fallback_models": [],
        "config.publish_output": False, "config.propagate_tool_errors": True,
        "model_routing.enable": False, "skills.enabled": False,
        "pr_update_changelog.push_changelog_changes": False,
    }.items():
        settings.set(key, value)
    if not scoped:
        monkeypatch.setattr("pr_agent.config_loader.global_settings", settings)
    encoder = SimpleNamespace(encode=lambda text, **_kwargs: list(text), decode=lambda tokens: "".join(tokens))
    monkeypatch.setattr(TokenEncoder, "get_token_encoder", lambda *_args, **_kwargs: encoder)

    provider = MagicMock()
    provider.pr.title = "Change a value"
    provider.get_languages.return_value = {"Python": 100}
    provider.get_files.return_value = ["app.py"] if has_files else []
    provider.get_pr_description.return_value = "Change the value from 1 to 2."
    provider.get_pr_branch.return_value = "change-value"
    provider.get_pr_id.return_value = "org/repo/1"
    provider.get_commit_messages.return_value = "Update the value"
    provider.get_pr_file_content.return_value = ""
    provider.get_filtered_diff_file_names.return_value = []
    provider.is_supported.return_value = False
    provider.supports_threaded_pr_questions.return_value = False
    monkeypatch.setattr(module, "get_git_provider", lambda: lambda _url: provider)
    handler = SimpleNamespace(chat_completion=AsyncMock(return_value=(response, "stop")))
    started = threading.Event()
    release = threading.Event()
    loop_thread = threading.get_ident()

    def fetch_diff():
        started.set()
        assert (threading.get_ident() != loop_thread) is scoped
        assert get_settings() is settings
        if scoped:
            assert release.wait(10)
        return [FilePatchInfo(
            "value = 1\n", "value = 2\n", "@@ -1 +1 @@\n-value = 1\n+value = 2\n",
            "app.py", edit_type=EDIT_TYPE.MODIFIED,
        )] if has_files else []

    provider.get_diff_files.side_effect = fetch_diff
    settings_scope = request_cycle_context({"settings": settings}) if scoped else nullcontext()
    with settings_scope:
        tool = tool_class("https://example.com/org/repo/pull/1", ai_handler=lambda: handler)
        if scoped:
            run = asyncio.create_task(tool.run())
            try:
                assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), 10)
                assert not run.done()
            finally:
                release.set()
                await asyncio.wait_for(run, 10)
        else:
            await tool.run()

    provider.get_diff_files.assert_called_once_with()
    if has_files:
        handler.chat_completion.assert_awaited_once()
        assert "+value = 2" in tool.patches_diff
    else:
        handler.chat_completion.assert_not_awaited()
