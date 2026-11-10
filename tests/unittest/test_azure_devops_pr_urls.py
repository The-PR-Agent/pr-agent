from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pr_agent.git_providers.azuredevops_provider import AzureDevopsProvider
from pr_agent.servers import azuredevops_server_webhook as webhook


def test_provider_without_a_pr_has_no_url():
    assert AzureDevopsProvider.__new__(AzureDevopsProvider).get_pr_url() == ""


@pytest.mark.parametrize("base", [
    "https://dev.azure.com/org",
    "https://org.visualstudio.com",
    "http://server.test:8080/tfs/My%20Collection",
])
@pytest.mark.parametrize("path, encoded_path", [
    ("My Project/_git/My Repo/pullrequest/1", "My%20Project/_git/My%20Repo/pullrequest/1"),
    ("My%20Project/_git/My%20Repo/pullrequest/1", "My%20Project/_git/My%20Repo/pullrequest/1"),
    ("My Project/_git/My%20Repo/pullrequest/1", "My%20Project/_git/My%20Repo/pullrequest/1"),
    ("Project/_git/repo/pullrequest/1", "Project/_git/repo/pullrequest/1"),
    ("Release%2520Plans/_git/repo%2Fpart%2520/pullrequest/1",
     "Release%2520Plans/_git/repo%2Fpart%2520/pullrequest/1"),
    ("研发 Project/_git/repo(name)/pullrequest/1",
     "%E7%A0%94%E5%8F%91%20Project/_git/repo%28name%29/pullrequest/1"),
])
def test_pr_file_and_comment_links_encode_path_segments_once(base, path, encoded_path):
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    provider.pr_url = f"{base}/{path}"
    expected = f"{base}/{encoded_path}"

    assert provider.get_pr_url() == expected
    assert provider.get_comment_url(SimpleNamespace(thread_id=2)) == f"{expected}?discussionId=2"
    assert provider.get_line_link("/src/file name.py", 3, 9) == (
        f"{expected}?_a=files&path=%2Fsrc%2Ffile%20name.py"
    )
    provider.pr_url = expected
    assert provider.get_pr_url() == expected


@pytest.mark.parametrize("project, repo, decoded_project, decoded_repo", [
    ("Dev%20Project", "My%20Repo", "Dev Project", "My Repo"),
    ("Dev%2520Project", "repo%2520name", "Dev%20Project", "repo%20name"),
    ("Dev%23Project", "repo%3Fname", "Dev#Project", "repo?name"),
])
@pytest.mark.parametrize("event", ["created", "comment"])
async def test_webhook_dispatch_retains_encoded_identity(
    monkeypatch, project, repo, decoded_project, decoded_repo, event,
):
    web_url = f"https://dev.azure.com/org/{project}/_git/{repo}/pullrequest/1"
    if event == "created":
        data = {"eventType": "git.pullrequest.created", "resource": {
            "_links": {"web": {"href": web_url.replace("/_git/", "/_apis/git/repositories/")}},
        }}
        dispatch = AsyncMock()
        monkeypatch.setattr(webhook, "_perform_commands_azure", dispatch)
        monkeypatch.setattr(webhook, "PRAgent", Mock())
    else:
        data = {
            "eventType": "ms.vss-code.git-pullrequest-comment-event", "resourceVersion": "2.0",
            "resource": {
                "comment": {"content": "/review", "id": 3,
                            "_links": {"threads": {"href": "https://example.test/threads/2"}}},
                "pullRequest": {"pullRequestId": 1, "repository": {
                    "webUrl": f"https://dev.azure.com/org/{project}/_git/{repo}",
                }},
            },
        }
        dispatch = AsyncMock()
        monkeypatch.setattr(webhook, "handle_request_comment", dispatch)

    response = await webhook.handle_request_azure(data, {})

    assert response.status_code == 202
    dispatch.assert_awaited_once()
    received_url = dispatch.await_args.args[2 if event == "created" else 0]
    assert received_url == web_url
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    provider._get_pr = Mock()
    provider.set_pr(received_url)
    assert provider.get_pr_url() == web_url
    assert (provider.workspace_slug, provider.repo_slug, provider.pr_num) == (decoded_project, decoded_repo, 1)
