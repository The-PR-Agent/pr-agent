"""Exercise ticket extraction through python-gitlab's real HTTP request construction."""
import asyncio
import json
from types import SimpleNamespace
from urllib.parse import urlparse

import gitlab
import pytest
import requests
from requests.adapters import BaseAdapter

from pr_agent.tools import ticket_pr_compliance_check as tickets


class IssueTransport(BaseAdapter):
    def __init__(self, outcomes=()):
        super().__init__()
        self.requests = []
        self.outcomes = iter(outcomes)

    def send(self, request, **kwargs):
        self.requests.append(request)
        outcome = next(self.outcomes, 200)
        if isinstance(outcome, BaseException):
            raise outcome
        # A project metadata request must never reach the transport.
        assert request.method == "GET" and "/issues/" in request.url
        iid = int(urlparse(request.url).path.rsplit("/", 1)[-1])
        payload = {
            "id": iid + 100,
            "iid": iid,
            "web_url": f"https://gitlab.example/group/repo/-/issues/{iid}",
            "title": f"Issue {iid}",
            "description": "x" * (tickets.MAX_TICKET_CHARACTERS + 1),
            "labels": ["bug", "backend"],
        } if outcome == 200 else {"message": "unavailable"}
        response = requests.Response()
        response.status_code = outcome
        response._content = json.dumps(payload).encode()
        response.headers["Content-Type"] = "application/json"
        response.request = request
        return response

    def close(self):
        pass


@pytest.fixture
def provider_factory(monkeypatch):
    # Keep unrelated integrations deterministic; retain the native extraction path.
    monkeypatch.setattr(tickets, "add_jira_tickets", lambda provider, content: content)
    sessions = []

    def make(description, outcomes=(), token="offline-token", retry=False):
        transport = IssueTransport(outcomes)
        session = requests.Session()
        session.mount("https://", transport)
        sessions.append(session)
        client = gitlab.Gitlab("https://gitlab.example/gitlab", private_token=token, session=session,
                               retry_transient_errors=retry)
        provider = SimpleNamespace(
            gl=client, id_project="group/repo", gitlab_url="https://gitlab.example/gitlab",
            get_user_description=lambda: description,
            supports_issue_url_tickets=lambda: False,
            supports_issue_reference_tickets=lambda: True,
        )
        return provider, transport

    yield make
    for session in sessions:
        session.close()


async def test_extract_issues_without_project_metadata(provider_factory):
    provider, transport = provider_factory("Fixes #1 #2 other/sub/project#3 #4 #1")
    result = await tickets.extract_tickets(provider)

    assert [ticket["ticket_id"] for ticket in result] == [1, 2, 3]
    assert [urlparse(request.url).path for request in transport.requests] == [
        "/gitlab/api/v4/projects/group%2Frepo/issues/1",
        "/gitlab/api/v4/projects/group%2Frepo/issues/2",
        "/gitlab/api/v4/projects/other%2Fsub%2Fproject/issues/3",
    ]
    assert all(request.headers["PRIVATE-TOKEN"] == "offline-token" for request in transport.requests)
    assert all(ticket["body"] == "x" * tickets.MAX_TICKET_CHARACTERS + "..." for ticket in result)
    assert all(ticket["labels"] == "bug, backend" for ticket in result)


@pytest.mark.parametrize("failure", [401, 403, 404, 500, requests.Timeout("offline timeout")])
async def test_issue_failures_preserve_successful_context(provider_factory, failure):
    provider, transport = provider_factory("Fixes #1 #2", [failure, 200])
    result = await tickets.extract_tickets(provider)

    assert [ticket["ticket_id"] for ticket in result] == [2]
    assert len(transport.requests) == 2


async def test_failed_issue_is_fetched_again_on_next_extraction(provider_factory):
    provider, transport = provider_factory("Fixes #1", [403, 200])
    assert await tickets.extract_tickets(provider) == []
    assert [ticket["ticket_id"] for ticket in await tickets.extract_tickets(provider)] == [1]
    assert len(transport.requests) == 2


async def test_gitlab_transport_retry_remains_enabled(provider_factory, monkeypatch):
    monkeypatch.setattr("gitlab.utils.time.sleep", lambda seconds: None)
    provider, transport = provider_factory("Fixes #1", [503, 200], retry=True)
    assert [ticket["ticket_id"] for ticket in await tickets.extract_tickets(provider)] == [1]
    assert len(transport.requests) == 2


async def test_cancellation_propagates_without_fetching_later_issues(provider_factory):
    provider, transport = provider_factory("Fixes #1 #2", [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await tickets.extract_tickets(provider)
    assert len(transport.requests) == 1


async def test_separate_credentials_do_not_share_results(provider_factory):
    allowed, allowed_transport = provider_factory("Fixes #1", token="allowed-offline-token")
    denied, denied_transport = provider_factory("Fixes #1", [403], token="denied-offline-token")
    assert len(await tickets.extract_tickets(allowed)) == 1
    assert await tickets.extract_tickets(denied) == []
    assert allowed_transport.requests[0].headers["PRIVATE-TOKEN"] == "allowed-offline-token"
    assert denied_transport.requests[0].headers["PRIVATE-TOKEN"] == "denied-offline-token"
