from unittest.mock import MagicMock

from pr_agent.git_providers.gitlab_provider import (
    MAX_PR_DESCRIPTION_GITLAB_LENGTH,
    GitLabProvider,
)


def _provider():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.mr = MagicMock()
    provider.mr.title = "Existing title"
    provider.id_mr = 1
    return provider


def test_publish_description_caps_a_body_over_the_api_limit():
    # An overlong /describe body would fail the whole mr.save(), including a
    # title that already fits.
    provider = _provider()
    body = "B" * (MAX_PR_DESCRIPTION_GITLAB_LENGTH + 200)
    title = "A short title"

    provider.publish_description(title, body)

    published = provider.mr.description
    assert provider.mr.title == title
    assert len(published) == MAX_PR_DESCRIPTION_GITLAB_LENGTH
    assert published.endswith(" ... (description truncated due to length limit)")
    assert published.startswith("B")
    provider.mr.save.assert_called_once()


def test_publish_description_keeps_a_body_within_the_limit():
    provider = _provider()
    body = "### **Description**\n- Short change\n"

    provider.publish_description("AI title", body)

    assert provider.mr.title == "AI title"
    assert provider.mr.description == body
    provider.mr.save.assert_called_once()


def test_publish_description_preserves_title_when_none():
    provider = _provider()

    provider.publish_description(None, "updated body")

    assert provider.mr.title == "Existing title"
    assert provider.mr.description == "updated body"
    provider.mr.save.assert_called_once()
