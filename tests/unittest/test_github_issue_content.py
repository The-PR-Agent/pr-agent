from unittest.mock import Mock

import pytest
from github import Auth, Github
from github.Repository import Repository

from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.github_provider import GithubProvider


@pytest.mark.parametrize("base_url", ["https://api.github.com", "https://ghe.example.test:8443/api/v3"])
@pytest.mark.parametrize("resolved_repo", ["org/repo", "ORG/Repo", "org/private"])
def test_issue_repository_is_checked_before_content_is_returned(monkeypatch, base_url, resolved_repo):
    client = Github(auth=Auth.Token("stub-token"), base_url=base_url, seconds_between_requests=0)
    try:
        repo_url = f"{base_url}/repos/org/repo"
        repo = Repository(client.requester, {}, {"url": repo_url, "full_name": "org/repo"}, completed=True)
        resolved_url = f"{base_url}/repos/{resolved_repo}"
        response = {
            "repository_url": resolved_url,
            "url": f"{resolved_url}/issues/1",
            "number": 1,
            "title": "Issue",
            "body": "Body",
            "labels": [{"name": "bug"}],
        }
        sdk_get = Mock(return_value=({}, response))
        monkeypatch.setattr(client.requester, "requestJsonAndCheck", sdk_get)
        provider = GithubProvider.__new__(GithubProvider)

        if resolved_repo == "org/private":
            with pytest.raises(ValueError, match="does not match the authorized repository"):
                provider.get_issue_content(repo, 1)
        else:
            issue = provider.get_issue_content(repo, 1)
            assert (issue.number, issue.title, issue.body, issue.pull_request) == (1, "Issue", "Body", None)
            assert [label.name for label in issue.labels] == ["bug"]
            assert issue.completed is True
        sdk_get.assert_called_once_with("GET", f"{repo_url}/issues/1", parameters=None, headers=None)
    finally:
        client.close()


def test_base_provider_fails_closed():
    with pytest.raises(NotImplementedError):
        GitProvider.get_issue_content(None, None, 1)
