"""Ticket context must be visible to the merge request's audience."""
import json
from types import SimpleNamespace
from urllib.parse import urlparse

import gitlab
import pytest
import requests
from requests.adapters import BaseAdapter

from pr_agent.commands import ticket_pr_compliance_check as tickets
from pr_agent.git_providers.gitlab_provider import GitLabProvider


class VisibilityTransport(BaseAdapter):
    def __init__(self, metadata, status=200):
        self.metadata = metadata
        self.status = status
        self.paths = []

    def send(self, request, **kwargs):
        path = urlparse(request.url).path
        self.paths.append(path)
        response = requests.Response()
        response.request = request
        response.headers["Content-Type"] = "application/json"
        if "/issues/" in path:
            iid = int(path.rsplit("/", 1)[-1])
            response.status_code = 200
            payload = {"iid": iid, "title": "Members' requirements", "description": "Restricted details",
                       "web_url": f"https://gitlab.example/group/repo/-/issues/{iid}", "labels": []}
        else:
            response.status_code = self.status
            payload = self.metadata
            if path.endswith("group%2Fother"):
                payload = self.sibling_metadata
        response._content = json.dumps(payload).encode()
        return response

    def close(self):
        pass


@pytest.mark.parametrize("visibility,issues_access_level,merge_requests_access_level,expected", [
    ("public", "private", None, []),
    ("internal", "private", None, []),
    ("public", "private", "enabled", []),
    ("public", "private", "private", [1, 2]),
    ("internal", "private", "private", [1, 2]),
    ("public", "disabled", "private", []),
    ("public", None, None, []),
    ("public", "unknown", None, []),
    (None, "enabled", None, []),
    ("public", "enabled", None, [1, 2]),
    ("internal", "enabled", None, [1, 2]),
    ("private", "enabled", None, [1, 2]),
    ("private", "private", None, [1, 2]),
])
async def test_issue_context_respects_project_visibility(
    monkeypatch, visibility, issues_access_level, merge_requests_access_level, expected
):
    monkeypatch.setattr(tickets, "add_jira_tickets", lambda provider, content: content)
    metadata = {
        "id": 1, "visibility": visibility, "issues_access_level": issues_access_level,
        "merge_requests_access_level": merge_requests_access_level,
    }
    transport = VisibilityTransport(metadata)
    with requests.Session() as session:
        session.mount("https://", transport)
        provider = GitLabProvider.__new__(GitLabProvider)
        provider.gl = gitlab.Gitlab("https://gitlab.example", private_token="offline-token", session=session)
        provider.id_project = "group/repo"
        provider.gitlab_url = "https://gitlab.example"
        provider.user_description = "Fixes #1 #2"
        provider.mr = SimpleNamespace(title="", description=provider.user_description)

        result = await tickets.extract_tickets(provider)

    assert [ticket["ticket_id"] for ticket in result] == expected
    assert transport.paths.count("/api/v4/projects/group%2Frepo") == 1
    assert len(transport.paths) == 1 + len(expected)


@pytest.mark.parametrize("status", [403, 404, 500])
async def test_unknown_project_access_skips_tickets_once_per_run(monkeypatch, status):
    monkeypatch.setattr(tickets, "add_jira_tickets", lambda provider, content: content)
    transport = VisibilityTransport({"message": "unavailable"}, status=status)
    with requests.Session() as session:
        session.mount("https://", transport)
        provider = GitLabProvider.__new__(GitLabProvider)
        provider.gl = gitlab.Gitlab("https://gitlab.example", private_token="offline-token", session=session)
        provider.id_project = "group/repo"
        provider.gitlab_url = "https://gitlab.example"
        provider.user_description = "Fixes #1 #2"
        provider.mr = SimpleNamespace(title="", description=provider.user_description)
        assert await tickets.extract_tickets(provider) == []
        assert transport.paths == ["/api/v4/projects/group%2Frepo"]
        # Failure is scoped to one extraction, so a later command can retry.
        transport.status = 200
        transport.metadata = {"id": 1, "visibility": "public", "issues_access_level": "enabled"}
        assert [ticket["ticket_id"] for ticket in await tickets.extract_tickets(provider)] == [1, 2]
        assert transport.paths.count("/api/v4/projects/group%2Frepo") == 2


@pytest.mark.parametrize("mr_visibility,sibling_visibility,issues_access_level,expected", [
    ("public", "public", "private", []),
    ("public", "internal", "enabled", []),
    ("internal", "public", "enabled", [1, 2]),
    ("public", "public", "enabled", [1, 2]),
    ("private", "private", "private", []),
])
async def test_sibling_issues_cover_mr_audience(
    monkeypatch, mr_visibility, sibling_visibility, issues_access_level, expected
):
    monkeypatch.setattr(tickets, "add_jira_tickets", lambda provider, content: content)
    transport = VisibilityTransport({"id": 1, "visibility": mr_visibility, "issues_access_level": "enabled"})
    transport.sibling_metadata = {
        "id": 2, "visibility": sibling_visibility, "issues_access_level": issues_access_level,
    }
    with requests.Session() as session:
        session.mount("https://", transport)
        provider = GitLabProvider.__new__(GitLabProvider)
        provider.gl = gitlab.Gitlab("https://gitlab.example", private_token="offline-token", session=session)
        provider.id_project = "group/repo"
        provider.gitlab_url = "https://gitlab.example"
        provider.user_description = "Fixes group/other#1 group/other#2"
        provider.mr = SimpleNamespace(title="", description=provider.user_description)
        # Authorize sibling resolution; the real SDK still fetches all metadata and issues.
        monkeypatch.setattr(provider, "is_sibling_repo_allowed", lambda *args, **kwargs: True)
        monkeypatch.setattr(provider, "_submodule_target_allowed", lambda project: True)

        result = await tickets.extract_tickets(provider)

    assert [ticket["ticket_id"] for ticket in result] == expected
    assert transport.paths.count("/api/v4/projects/group%2Fother") == 1
    assert transport.paths.count("/api/v4/projects/group%2Frepo") == 1
    assert len(transport.paths) == 2 + len(expected)
