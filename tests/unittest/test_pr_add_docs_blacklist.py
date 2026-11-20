import copy

import pytest
from starlette_context import request_cycle_context

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.run_details import command_failed
from pr_agent.config_loader import get_settings

APP_DIFF = (
    "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
    " def total(items):\n-    return 0\n+    return sum(items)\n"
)
SQL_DIFF = (
    "diff --git a/schema.sql b/schema.sql\n--- a/schema.sql\n+++ b/schema.sql\n@@ -1 +1 @@\n"
    "-CREATE TABLE orders (id INT);\n+CREATE TABLE orders (id INT, total INT);\n"
)


async def _run_add_docs(tmp_path, monkeypatch, diff):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    prompts = []

    class FakeAiHandler(BaseAiHandler):
        def __init__(self):
            pass

        @property
        def deployment_id(self):
            return "fake"

        async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
            prompts.append(user)
            return "Code Documentation: []\n", "stop"

    get_settings().get("config")
    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        for key, value in {
            "config.git_provider": "plain-diff",
            "config.model": "gpt-4o-mini",
            "config.fallback_models": [],
            "config.repo_context_files": [],
            "config.publish_output": False,
            "config.propagate_command_errors": True,
            "config.extra_config_url": "",
            "plain_diff.content": diff,
            "plain_diff.output_path": None,
            "plain_diff.json_output_path": None,
            "push_outputs.enable": False,
        }.items():
            get_settings().set(key, value)

        assert await PRAgent(ai_handler=FakeAiHandler).handle_request("local_diff", "/add_docs") is True
        assert command_failed() is False
    return prompts


@pytest.mark.asyncio
async def test_add_docs_leaves_blacklisted_files_out_of_the_prompt(tmp_path, monkeypatch):
    prompts = await _run_add_docs(tmp_path, monkeypatch, APP_DIFF + SQL_DIFF)

    assert len(prompts) == 1
    assert "app.py" in prompts[0]
    assert "return sum(items)" in prompts[0]
    assert "schema.sql" not in prompts[0]
    assert "CREATE TABLE" not in prompts[0]


@pytest.mark.asyncio
async def test_add_docs_skips_the_model_when_only_blacklisted_files_change(tmp_path, monkeypatch):
    assert await _run_add_docs(tmp_path, monkeypatch, SQL_DIFF) == []
