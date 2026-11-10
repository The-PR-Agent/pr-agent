import os
import sys

import pytest

# Match CI (#3475): use the LiteLLM cost map bundled with the version pinned in uv.lock rather than the copy
# LiteLLM fetches at import time, so local runs do not fail on third-party map changes (#3473, #3583).
# Respect an explicit LITELLM_LOCAL_MODEL_COST_MAP from the environment; set it to False to use the live map.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


@pytest.fixture(autouse=True)
def isolate_run_details():
    """Start each test from a clean run-details ContextVar and restore it.

    The collector lives in a module-level ContextVar, so a test that leaves
    details behind would otherwise be visible to whichever test runs next.
    """
    from pr_agent.algo import run_details

    token = run_details._run_details.set(None)
    yield
    run_details._run_details.reset(token)


@pytest.fixture(autouse=True)
def skip_llm_retry_waits(monkeypatch):
    """Keep chat_completion's same-model retry attempts, without the waits between them.

    Applies once the LiteLLM handler module is imported, which test modules do at collection.
    """
    handler_module = sys.modules.get("pr_agent.algo.ai_handlers.litellm_ai_handler")
    if handler_module is not None:

        async def no_sleep(seconds):
            pass

        monkeypatch.setattr(handler_module.LiteLLMAIHandler._chat_completion_with_retry.retry, "sleep", no_sleep)


@pytest.fixture
def github_issue_pages():
    """Supply real SDK issue pages and reject redundant issue-detail requests."""
    from unittest.mock import Mock

    from github.Repository import Repository

    requesters = []

    def make_list(numbers):
        repo_url = "https://api.github.com/repos/Example/Repo"
        list_url = f"{repo_url}/issues"
        payloads = [{
            "url": f"{list_url}/{number}", "number": number,
            "title": f"Issue {number}", "body": "Body",
            "user": {"login": "user"}, "created_at": "2020-01-01T00:00:00Z",
        } for number in numbers]
        payloads.insert(1, {
            "url": f"{list_url}/1000000", "number": 1000000,
            "pull_request": {"url": f"{repo_url}/pulls/1000000"},
        })
        requester = Mock(per_page=2, base_url="https://api.github.com", is_not_lazy=False)

        def request(method, url, **kwargs):
            page = requester.requestJsonAndCheck.call_count - 1
            assert method == "GET"
            assert url == list_url + (f"?page={page + 1}" if page else "")
            end = (page + 1) * 2
            headers = {"link": f'<{list_url}?page={page + 2}>; rel="next"'} if end < len(payloads) else {}
            return headers, payloads[page * 2:end]

        requester.requestJsonAndCheck.side_effect = request
        requesters.append((requester, (len(payloads) + 1) // 2))
        return Repository(requester, {}, {"url": repo_url}).get_issues(state="all")

    yield make_list

    for requester, page_count in requesters:
        assert requester.requestJsonAndCheck.call_count == page_count
