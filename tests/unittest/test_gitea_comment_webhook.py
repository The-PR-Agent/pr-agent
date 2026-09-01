"""Slash commands posted as PR comments on Gitea must reach the agent.

Gitea's issue_comment payload has no top-level ``pull_request`` key (unlike
``pull_request`` events). The parent PR lives under ``issue`` and
``issue.pull_request`` carries only merge metadata (``has_merged`` /
``merged``) — no URL. ``handle_comment_event`` must therefore synthesize the
PR URL from ``repository.full_name`` + ``issue.number`` against
``GITEA.URL``, and must keep ignoring non-PR comments and non-command text.
"""

import copy

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.servers import gitea_app


class UrlRecordingAgent:
    def __init__(self):
        self.calls = []  # list of (url, command)

    async def handle_request(self, url, command, notify=None):
        self.calls.append((url, command))


def _comment_payload(**overrides):
    payload = {
        "action": "created",
        "is_pull": True,
        "issue": {
            "number": 1091,
            "pull_request": {"has_merged": False, "merged": None},
            "url": "http://example/api/v1/repos/org/repo/issues/1091",
        },
        "comment": {"id": 42, "body": "/improve"},
        "repository": {"full_name": "org/repo"},
        "sender": {"login": "alice"},
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def gitea_url():
    settings = get_settings()
    original = copy.deepcopy(settings.get("GITEA"))
    settings.set("GITEA.URL", "http://gitea:3000")
    try:
        yield
    finally:
        settings.set("GITEA", original)


@pytest.mark.asyncio
async def test_issue_comment_on_pr_synthesizes_url(gitea_url):
    agent = UrlRecordingAgent()
    await gitea_app.handle_comment_event(_comment_payload(), "issue_comment", "created", agent)
    assert agent.calls == [("http://gitea:3000/org/repo/pulls/1091", "/improve")]


@pytest.mark.asyncio
async def test_issue_comment_on_plain_issue_is_ignored(gitea_url):
    agent = UrlRecordingAgent()
    payload = _comment_payload(is_pull=False, issue={"number": 7, "title": "bug"})
    await gitea_app.handle_comment_event(payload, "issue_comment", "created", agent)
    assert agent.calls == []


@pytest.mark.asyncio
async def test_top_level_pull_request_url_is_preferred(gitea_url):
    agent = UrlRecordingAgent()
    payload = {
        "comment": {"body": "/review"},
        "pull_request": {"url": "http://gitea:3000/o/r/pulls/5"},
    }
    await gitea_app.handle_comment_event(payload, "issue_comment", "created", agent)
    assert agent.calls == [("http://gitea:3000/o/r/pulls/5", "/review")]


@pytest.mark.asyncio
async def test_non_command_comment_is_ignored(gitea_url):
    agent = UrlRecordingAgent()
    payload = _comment_payload(comment={"id": 42, "body": "looks good"})
    await gitea_app.handle_comment_event(payload, "issue_comment", "created", agent)
    assert agent.calls == []


@pytest.mark.asyncio
async def test_malformed_payload_is_ignored_without_crashing(gitea_url):
    agent = UrlRecordingAgent()
    payload = _comment_payload(repository={})
    await gitea_app.handle_comment_event(payload, "issue_comment", "created", agent)
    assert agent.calls == []
