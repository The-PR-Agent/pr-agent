"""Exercise ticket extraction through python-gitlab's real HTTP request construction."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlparse

import gitlab
import pytest
import requests
from requests.adapters import BaseAdapter

from pr_agent.git_providers.gitlab_provider import GitLabProvider
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
        if isinstance(outcome, tuple):
            outcome, payload = outcome
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
        provider = GitLabProvider.__new__(GitLabProvider)
        provider.gl = client
        provider.id_project = "group/repo"
        provider.gitlab_url = "https://gitlab.example/gitlab"
        provider.user_description = description
        provider.mr = SimpleNamespace(title="", description=description, author={"id": 42})
        return provider, transport

    yield make
    for session in sessions:
        session.close()


async def test_extract_issues_without_project_metadata(provider_factory):
    provider, transport = provider_factory("Fixes #1 #2 #4 #1")
    result = await tickets.extract_tickets(provider)

    assert [ticket["ticket_id"] for ticket in result] == [1, 2, 4]
    assert [urlparse(request.url).path for request in transport.requests] == [
        "/gitlab/api/v4/projects/group%2Frepo/issues/1",
        "/gitlab/api/v4/projects/group%2Frepo/issues/2",
        "/gitlab/api/v4/projects/group%2Frepo/issues/4",
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


@pytest.mark.parametrize("payload", [{}, {"iid": 2, "description": "partial issue"}])
@pytest.mark.asyncio
async def test_malformed_issue_preserves_earlier_successes_and_refills(provider_factory, payload):
    provider, transport = provider_factory("Fixes #1 #2 #3 #4 #5", [200, (200, payload), 200, 200, 200])
    result = await tickets.extract_tickets(provider)

    assert [ticket["ticket_id"] for ticket in result] == [1, 3, 4]
    assert [int(urlparse(request.url).path.rsplit("/", 1)[-1]) for request in transport.requests] == [1, 2, 3, 4]


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


@pytest.mark.asyncio
async def test_failed_initial_references_are_replaced_with_later_tickets(provider_factory):
    provider, transport = provider_factory("Fixes #1 #2 #3 #4 #5 #6 #7", [404, 403, 500, 200, 200, 200])
    result = await tickets.extract_tickets(provider)

    assert [ticket["ticket_id"] for ticket in result] == [4, 5, 6]
    assert len(transport.requests) == 6


@pytest.mark.asyncio
async def test_successful_ticket_limit_stops_later_lookups(provider_factory):
    provider, transport = provider_factory("Fixes #1 #2 #3 #4", [200, 200, 200, 404])
    assert [ticket["ticket_id"] for ticket in await tickets.extract_tickets(provider)] == [1, 2, 3]
    assert len(transport.requests) == 3


@pytest.mark.asyncio
async def test_failed_ticket_lookup_budget_is_ten_distinct_references(provider_factory):
    provider, transport = provider_factory("Fixes " + " ".join(f"#{iid}" for iid in range(1, 12)), [404] * 11)
    assert await tickets.extract_tickets(provider) == []
    assert [int(urlparse(request.url).path.rsplit("/", 1)[-1]) for request in transport.requests] == list(range(1, 11))


@pytest.mark.asyncio
async def test_duplicate_references_do_not_consume_lookup_budget(provider_factory):
    description = "#1 GROUP/REPO#1 #2 #2 #3 #4"
    provider, transport = provider_factory(description, [404, 404, 404, 200])
    assert [ticket["ticket_id"] for ticket in await tickets.extract_tickets(provider)] == [4]
    assert len(transport.requests) == 4


@pytest.mark.asyncio
async def test_logical_lookup_budget_preserves_sdk_transient_retries(provider_factory, monkeypatch):
    monkeypatch.setattr("gitlab.utils.time.sleep", lambda seconds: None)
    provider, transport = provider_factory("Fixes #1 #2 #3 #4", [503] * 9 + [200, 200, 200], retry=True)
    assert [ticket["ticket_id"] for ticket in await tickets.extract_tickets(provider)] == [1, 2, 3]
    assert len(transport.requests) == 12


@pytest.mark.asyncio
async def test_cancellation_during_refill_stops_later_references(provider_factory):
    provider, transport = provider_factory("Fixes #1 #2 #3 #4 #5", [404, 404, 404, asyncio.CancelledError(), 200])
    with pytest.raises(asyncio.CancelledError):
        await tickets.extract_tickets(provider)
    assert len(transport.requests) == 4


@pytest.mark.asyncio
async def test_other_ticket_integrations_remain_outside_gitlab_success_limit(provider_factory, monkeypatch):
    asana = {"ticket_id": "asana-1"}
    jira = {"ticket_id": "jira-1"}

    async def fetch_asana(*args):
        return [asana]

    monkeypatch.setattr(tickets, "_fetch_asana_ticket_contents", fetch_asana)
    monkeypatch.setattr(tickets, "add_jira_tickets", lambda provider, content: content.append(jira))
    provider, transport = provider_factory("#1 #2 #3 #4", [404, 200, 200, 200])
    result = await tickets.extract_tickets(provider)
    assert [ticket["ticket_id"] for ticket in result] == [2, 3, 4, "asana-1", "jira-1"]
    assert len(transport.requests) == 4


def _sibling_project(path="group/sub/project", visibility="public", confidential=False):
    project = Mock()
    project.path_with_namespace = path
    project.visibility = visibility
    project.issues.get.return_value = SimpleNamespace(
        iid=3,
        web_url="https://gitlab.example/group/sub/project/-/issues/3",
        title="Issue 3",
        description="sibling issue",
        labels=["bug"],
        confidential=confidential,
    )
    return project


async def test_allowlisted_sibling_issue_is_fetched(provider_factory, monkeypatch):
    provider, transport = provider_factory("Fixes other/sub/project#3")
    project = _sibling_project()
    monkeypatch.setattr(provider, "_project_by_path", Mock(return_value=project))
    monkeypatch.setattr(provider, "get_owning_namespace", Mock(return_value="group"))
    monkeypatch.setattr(provider, "is_sibling_repo_allowed", Mock(return_value=True))

    result = await tickets.extract_tickets(provider)

    assert [ticket["ticket_id"] for ticket in result] == [3]
    assert transport.requests == []
    project.issues.get.assert_called_once_with(3)


async def test_confidential_sibling_issue_is_skipped(provider_factory, monkeypatch):
    provider, transport = provider_factory("Fixes other/sub/project#3")
    project = _sibling_project(confidential=True)
    monkeypatch.setattr(provider, "_project_by_path", Mock(return_value=project))
    monkeypatch.setattr(provider, "get_owning_namespace", Mock(return_value="group"))
    monkeypatch.setattr(provider, "is_sibling_repo_allowed", Mock(return_value=True))

    with patch("pr_agent.tools.ticket_pr_compliance_check.get_logger") as logger:
        result = await tickets.extract_tickets(provider)

    assert result == []
    assert transport.requests == []
    project.issues.get.assert_called_once_with(3)
    logger.return_value.warning.assert_called_once_with(
        "Ignoring confidential cross-project GitLab issue: other/sub/project#3"
    )


async def test_unapproved_sibling_issue_is_not_resolved(provider_factory, monkeypatch):
    provider, transport = provider_factory("Fixes other/sub/project#3")
    project_by_path = Mock()
    monkeypatch.setattr(provider, "_project_by_path", project_by_path)
    monkeypatch.setattr(provider, "is_sibling_repo_allowed", Mock(return_value=False))

    with patch("pr_agent.git_providers.gitlab_provider.get_logger") as logger:
        result = await tickets.extract_tickets(provider)

    assert result == []
    assert transport.requests == []
    project_by_path.assert_not_called()
    logger.return_value.warning.assert_called_once_with(
        "Ignoring sibling repo absent from the host allowlist: other/sub/project"
    )


@pytest.mark.parametrize("owning_namespace, visibility, access_level", [
    ("other-group", "public", None),
    ("group", "private", 10),
])
async def test_unauthorized_sibling_issue_is_skipped(
    provider_factory, monkeypatch, owning_namespace, visibility, access_level
):
    provider, transport = provider_factory("Fixes other/sub/project#3")
    project = _sibling_project(visibility=visibility)
    if access_level is not None:
        project.members_all.get.return_value = SimpleNamespace(access_level=access_level)
    monkeypatch.setattr(provider, "_project_by_path", Mock(return_value=project))
    monkeypatch.setattr(provider, "get_owning_namespace", Mock(return_value=owning_namespace))
    monkeypatch.setattr(provider, "is_sibling_repo_allowed", Mock(return_value=True))

    result = await tickets.extract_tickets(provider)

    assert result == []
    assert transport.requests == []
    project.issues.get.assert_not_called()
