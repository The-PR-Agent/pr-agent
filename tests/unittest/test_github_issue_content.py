from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from github import Auth, Github, GithubException
from requests.exceptions import Timeout

from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.tools import ticket_pr_compliance_check as tpc


@pytest.fixture
def provider(monkeypatch):
    provider = GithubProvider.__new__(GithubProvider)
    provider.github_client = Github(auth=Auth.Token("stub-token"), seconds_between_requests=0)
    provider.repo = "org/repo"
    provider.repo_obj = SimpleNamespace(full_name="org/repo")
    provider.base_url_html = "https://github.com"
    provider.get_user_description = lambda: "Fixes #1"
    provider.get_pr_branch = lambda: "main"
    provider.fetch_sub_issues = Mock(return_value=[])
    monkeypatch.setattr(tpc, "_fetch_asana_ticket_contents", AsyncMock(return_value=[]))
    yield provider
    provider.github_client.close()


def _response(provider, number=1, repo="org/repo", status=200):
    repo_url = f"{provider.github_client.requester.base_url}/repos/{repo}"
    data = {"repository_url": repo_url, "url": f"{repo_url}/issues/{number}", "number": number,
            "title": "Issue", "body": "Body", "labels": [{"name": "bug"}]}
    return Mock(status_code=status, headers={}, json=Mock(return_value=data))


@pytest.mark.parametrize("base_url", ["https://api.github.com", "https://ghe.example.test:8443/api/v3"])
def test_issue_uses_configured_api_and_auth_without_lazy_requests(provider, monkeypatch, base_url):
    provider.github_client.close()
    provider.github_client = Github(auth=Auth.Token("stub-token"), base_url=base_url, timeout=17,
                                    verify="custom-ca.pem", user_agent="test-agent", api_version="2022-11-28")
    response = _response(provider)
    get = Mock(return_value=response)
    monkeypatch.setattr("pr_agent.git_providers.github_provider.requests.get", get)
    sdk_get = Mock(side_effect=AssertionError("Issue properties must not refetch"))
    monkeypatch.setattr(provider.github_client.requester, "requestJsonAndCheck", sdk_get)

    issue = provider.get_issue_content(provider.repo_obj, 1)
    assert (issue.number, issue.title, issue.body, issue.pull_request) == (1, "Issue", "Body", None)
    assert [label.name for label in issue.labels] == ["bug"]
    assert issue.completed is True
    get.assert_called_once_with(
        f"{base_url}/repos/org/repo/issues/1", allow_redirects=False, timeout=17, verify="custom-ca.pem",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "test-agent",
                 "Authorization": "token stub-token", "X-GitHub-Api-Version": "2022-11-28"},
    )
    sdk_get.assert_not_called()
    response.close.assert_called_once()


def test_installation_auth_refreshes_token(provider, monkeypatch):
    auth = Auth.AppInstallationAuth(Auth.AppAuth("123", "stub-key"), installation_id=7)
    refresh = Mock(return_value=SimpleNamespace(token="installation-token",
                                               expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
    monkeypatch.setattr(auth, "_get_installation_authorization", refresh)
    provider.github_client.close()
    provider.github_client = Github(auth=auth)
    get = Mock(return_value=_response(provider))
    monkeypatch.setattr("pr_agent.git_providers.github_provider.requests.get", get)

    provider.get_issue_content(provider.repo_obj, 1)
    assert get.call_args.kwargs["headers"]["Authorization"] == "token installation-token"
    refresh.assert_called_once_with()


@pytest.mark.parametrize("status", [301, 302, 307, 308, 403, 404, 429, 500])
def test_non_success_never_parses_or_follows_response(provider, monkeypatch, status):
    response = _response(provider, status=status)
    response.headers = {"Location": "https://api.github.com/repos/org/private/issues/9"}
    get = Mock(return_value=response)
    monkeypatch.setattr("pr_agent.git_providers.github_provider.requests.get", get)

    with pytest.raises(GithubException) as error:
        provider.get_issue_content(provider.repo_obj, 1)
    assert error.value.status == status
    get.assert_called_once()
    assert get.call_args.kwargs["allow_redirects"] is False
    response.json.assert_not_called()
    response.close.assert_called_once()


@pytest.mark.parametrize("field,value", [
    ("repository_url", "https://api.github.com/repos/org/private"),
    ("repository_url", "https://other.example.test/repos/org/repo"),
    ("repository_url", None),
    ("url", "https://api.github.com/repos/org/private/issues/1"),
    ("url", "https://api.github.com/repos/org/repo/issues/9"),
    ("url", None),
    ("number", 9),
    ("number", True),
])
def test_canonical_identity_mismatch_is_rejected(provider, monkeypatch, field, value):
    response = _response(provider)
    response.json.return_value[field] = value
    monkeypatch.setattr("pr_agent.git_providers.github_provider.requests.get", Mock(return_value=response))
    materialize = Mock(side_effect=AssertionError("Rejected content must not be materialized"))
    monkeypatch.setattr("pr_agent.git_providers.github_provider.Issue", materialize)

    with pytest.raises(ValueError):
        provider.get_issue_content(provider.repo_obj, 1)
    materialize.assert_not_called()
    response.close.assert_called_once()


def test_case_variant_canonical_identity_is_accepted(provider, monkeypatch):
    response = _response(provider, repo="ORG/Repo")
    monkeypatch.setattr("pr_agent.git_providers.github_provider.requests.get", Mock(return_value=response))
    assert provider.get_issue_content(provider.repo_obj, 1).number == 1


@pytest.mark.parametrize("repo", [None, "org/../private", "org/..", "org/repo?x=1"])
def test_invalid_repository_cannot_change_request_destination(provider, monkeypatch, repo):
    get = Mock()
    monkeypatch.setattr("pr_agent.git_providers.github_provider.requests.get", get)
    with pytest.raises(ValueError):
        provider.get_issue_content(SimpleNamespace(full_name=repo), 1)
    get.assert_not_called()


def test_transport_error_is_propagated(provider, monkeypatch):
    monkeypatch.setattr("pr_agent.git_providers.github_provider.requests.get", Mock(side_effect=Timeout()))
    with pytest.raises(Timeout):
        provider.get_issue_content(provider.repo_obj, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [True, False])
async def test_redirected_parent_or_child_is_omitted_before_content_read(provider, monkeypatch, parent):
    response = _response(provider, number=1 if parent else 2, status=301)
    response.headers = {"Location": "https://api.github.com/repos/org/private/issues/9"}
    responses = [response] if parent else [_response(provider), response]
    get = Mock(side_effect=responses)
    monkeypatch.setattr("pr_agent.git_providers.github_provider.requests.get", get)
    provider.fetch_sub_issues.return_value = ["https://github.com/org/repo/issues/2"]

    tickets = await tpc.extract_tickets(provider)
    if parent:
        assert tickets == []
        provider.fetch_sub_issues.assert_not_called()
    else:
        assert [ticket["ticket_id"] for ticket in tickets] == [1]
        assert tickets[0]["sub_issues"] == []
    assert get.call_count == len(responses)
    assert all(call.kwargs["allow_redirects"] is False for call in get.call_args_list)
    response.json.assert_not_called()


def test_base_provider_fails_closed():
    with pytest.raises(NotImplementedError):
        GitProvider.get_issue_content(None, None, 1)
