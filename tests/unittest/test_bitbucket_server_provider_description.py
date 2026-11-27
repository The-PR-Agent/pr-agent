from types import SimpleNamespace
from unittest.mock import MagicMock

from pr_agent.git_providers.bitbucket_server_provider import (
    MAX_PR_DESCRIPTION_BITBUCKET_SERVER_LENGTH,
    BitbucketServerProvider,
)


def _provider():
    provider = BitbucketServerProvider.__new__(BitbucketServerProvider)
    provider.workspace_slug = "AAA"
    provider.repo_slug = "my-repo"
    provider.pr_num = 1
    provider.pr = SimpleNamespace(version=4, title="Existing title", reviewers=["alice"])
    provider.bitbucket_client = MagicMock()
    return provider


def test_publish_description_caps_a_body_over_the_api_limit():
    # PullRequest.getDescription() is @OptionalString(size=32768). An overlong
    # /describe body would fail the whole update, including a title that fits.
    provider = _provider()
    body = "B" * (MAX_PR_DESCRIPTION_BITBUCKET_SERVER_LENGTH + 200)
    title = "A short title"

    provider.publish_description(title, body)

    payload = provider.bitbucket_client.update_pull_request.call_args.args[3]
    published = payload["description"]
    assert payload["title"] == title
    assert len(published) == MAX_PR_DESCRIPTION_BITBUCKET_SERVER_LENGTH
    assert published.endswith(" ... (description truncated due to length limit)")
    assert published.startswith("B")


def test_publish_description_keeps_a_body_within_the_limit():
    provider = _provider()
    body = "### **Description**\n- Short change\n"

    provider.publish_description("AI title", body)

    payload = provider.bitbucket_client.update_pull_request.call_args.args[3]
    assert payload["description"] == body
    assert payload["title"] == "AI title"
    assert payload["version"] == 4
    assert payload["reviewers"] == ["alice"]


def test_publish_description_preserves_title_when_none():
    provider = _provider()
    provider._get_pr = MagicMock(return_value=SimpleNamespace(
        version=5, title="Edited during describe", reviewers=[]
    ))

    provider.publish_description(None, "updated body")

    payload = provider.bitbucket_client.update_pull_request.call_args.args[3]
    assert payload["title"] == "Edited during describe"
    assert payload["description"] == "updated body"
    assert payload["version"] == 5
