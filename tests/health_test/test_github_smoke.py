import asyncio
import copy
import os

from starlette_context import context, request_cycle_context

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.config_loader import get_settings, global_settings

# The default target is linked to #2934, so the smoke run
# exercises the ticket extraction path against live data. Override with
# TEST_PR_URL; assertions specific to the default target are then skipped.
DEFAULT_PR_URL = 'https://github.com/The-PR-Agent/pr-agent/pull/2940'

DESCRIBE_STUB_YAML = """\
title: |
  fix: guard against empty commit list in Azure DevOps get_latest_commit_url
type: Enhancement
description: |
  Synthetic PR description produced by the CI smoke stub so the run needs no model key.
pr_files:
  - filename: |
      pr_agent/git_providers/azuredevops_provider.py
    changes_title: |
      Guard the empty commit list
    changes_summary: |
      Return an empty string instead of raising IndexError when the commit list is empty
    label: |
      Enhancement
  - filename: |
      tests/unittest/test_azure_devops_comment.py
    changes_title: |
      Add empty-commit regression coverage
    changes_summary: |
      Cover get_latest_commit_url with an empty commit list
    label: |
      Tests
"""


class StubDescribeHandler(BaseAiHandler):
    """Deterministic AI handler for the GitHub smoke test.

    Returns fixed YAML valid for the /describe pipeline, so the run exercises
    provider auth, diff building, language sorting, ticket extraction and prompt
    rendering end to end without any model key.
    """

    def __init__(self):
        self.main_pr_language = "Python"
        self.calls = []

    @property
    def deployment_id(self):
        return "smoke-stub"

    async def chat_completion(self, model: str, system: str, user: str,
                              temperature: float = 0.2, img_path: str = None):
        self.calls.append((system, user))
        return DESCRIBE_STUB_YAML, "stop"


def _extract_diff_section(user_prompt: str) -> str:
    """Return the rendered diff body between the prompt's fixed markers."""
    start_marker = "The PR Git Diff:"
    end_marker = "Note that lines in the diff body are prefixed"
    start = user_prompt.find(start_marker)
    end = user_prompt.find(end_marker)
    assert start != -1 and end != -1 and end > start, "describe prompt is missing the diff section"
    return user_prompt[start + len(start_marker):end].strip("=\n ").strip()


async def _run_smoke() -> None:
    pr_url = os.getenv('TEST_PR_URL', DEFAULT_PR_URL)

    get_settings().set("config.git_provider", "github")
    get_settings().set("config.publish_output", False)
    get_settings().set("config.fallback_models", [])
    get_settings().set("config.propagate_tool_errors", True)

    stub = StubDescribeHandler()
    agent = PRAgent(ai_handler=lambda: stub)
    ok = await agent.handle_request(pr_url, ['describe'])

    assert ok, "describe command failed against the target GitHub PR"
    assert stub.calls, "the AI handler was never invoked - describe did not reach the model boundary"

    system_prompt, user_prompt = stub.calls[0]
    assert isinstance(system_prompt, str) and system_prompt.strip(), "describe system prompt rendered empty"
    diff_section = _extract_diff_section(user_prompt)
    assert diff_section, "describe prompt diff section is empty"

    if pr_url == DEFAULT_PR_URL:
        assert "azuredevops_provider.py" in diff_section, \
            "describe prompt is missing the default target PR diff content"
        assert "Related Ticket Info:" in user_prompt, "describe prompt is missing the related-ticket section"
        assert "Ticket Title: 'Azure DevOps get_latest_commit_url" in user_prompt, \
            "expanded ticket data did not reach the describe prompt"

    artifact = dict(get_settings().data).get('artifact', '')
    assert isinstance(artifact, str) and artifact.startswith("###"), \
        f"Expected markdown artifact, got {type(artifact).__name__}"
    assert "PR Type" in artifact, "PR description artifact missing the PR Type section"
    assert "Description" in artifact, "PR description artifact missing the Description section"


def test_github_describe_smoke():
    global_settings.get("config")  # force the lazy load before copying settings
    with request_cycle_context({}):
        context['settings'] = copy.deepcopy(global_settings)
        asyncio.run(_run_smoke())


if __name__ == '__main__':
    test_github_describe_smoke()
