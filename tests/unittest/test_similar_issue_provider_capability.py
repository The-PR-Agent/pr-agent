from types import SimpleNamespace

import pytest

from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.tools.pr_similar_issue import PRSimilarIssue, _provider_supports_issue_indexing


class UnsupportedProvider(GitProvider):
    """A provider class that does not declare the issue-indexing capability."""

    def is_supported(self, capability: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_similar_issue_unsupported_provider_publishes_message(monkeypatch):
    class FakeProvider:
        def __init__(self):
            self.comments = []

        def publish_comment(self, body):
            self.comments.append(body)

    fake_provider = FakeProvider()

    class FakeSettings:
        class config:
            git_provider = "gitlab"
            publish_output = True

    monkeypatch.setattr("pr_agent.tools.pr_similar_issue.get_settings", lambda: FakeSettings)
    monkeypatch.setattr("pr_agent.tools.pr_similar_issue.get_git_provider", lambda: UnsupportedProvider)
    monkeypatch.setattr(
        "pr_agent.git_providers.get_git_provider_with_context",
        lambda _: fake_provider,
    )

    tool = PRSimilarIssue("https://gitlab.example.com/group/repo/-/merge_requests/1", None)
    result = await tool.run()

    assert result == ""
    assert fake_provider.comments == [
        "The /similar_issue tool is not supported by the configured git provider."
    ]


@pytest.mark.asyncio
async def test_similar_issue_unsupported_provider_no_publish(monkeypatch):
    class FakeSettings:
        class config:
            git_provider = "gitlab"
            publish_output = False

    monkeypatch.setattr("pr_agent.tools.pr_similar_issue.get_settings", lambda: FakeSettings)
    monkeypatch.setattr("pr_agent.tools.pr_similar_issue.get_git_provider", lambda: UnsupportedProvider)

    tool = PRSimilarIssue("https://gitlab.example.com/group/repo/-/merge_requests/1", None)
    result = await tool.run()

    assert result == ""


def test_github_provider_declares_issue_indexing():
    """The capability replaces the previous `git_provider == "github"` string comparison."""
    assert GithubProvider.supports_issue_indexing() is True


def test_git_provider_default_is_unsupported():
    """A provider that does not override the capability is treated as unsupported."""
    assert GitProvider.supports_issue_indexing() is False
    assert UnsupportedProvider.supports_issue_indexing() is False


def test_capability_is_read_off_the_registered_class(monkeypatch):
    """A provider registered from outside the package is judged by what it declares."""

    class ThirdPartyProvider(GitProvider):
        def is_supported(self, capability: str) -> bool:
            return False

        @classmethod
        def supports_issue_indexing(cls) -> bool:
            return True

    monkeypatch.setattr("pr_agent.tools.pr_similar_issue.get_git_provider", lambda: ThirdPartyProvider)
    assert _provider_supports_issue_indexing() is True


def test_declared_capability_beats_the_configured_provider_id(monkeypatch):
    """A non-`github` id that declares the capability is supported.

    This is the behavior change: the previous `config.git_provider == "github"` comparison
    turned any other id away regardless of what the provider could do. The provider is built and
    interrupted at its GitHub client hook, so the test observes that the guard let it through
    without running the indexing path.
    """

    class Sentinel(Exception):
        pass

    class CapableProvider(GithubProvider):
        def _get_github_client(self):
            raise Sentinel

        @classmethod
        def supports_issue_indexing(cls) -> bool:
            return True

    class FakeSettings:
        class config:
            git_provider = "my-forge"
            publish_output = False
            CLI_MODE = True

        CONFIG = config
        pr_similar_issue = SimpleNamespace(max_issues_to_scan=10, vectordb="pinecone")

    monkeypatch.setattr("pr_agent.tools.pr_similar_issue.get_settings", lambda: FakeSettings)
    monkeypatch.setattr("pr_agent.tools.pr_similar_issue.get_git_provider", lambda: CapableProvider)

    with pytest.raises(Sentinel):
        PRSimilarIssue("https://my-forge.example.com/group/repo/issues/1", None)


def test_unknown_provider_configuration_is_unsupported(monkeypatch):
    """An unresolvable provider id is reported as unsupported rather than raised."""

    def raise_unknown():
        raise ValueError("Unknown git provider: nope")

    monkeypatch.setattr("pr_agent.tools.pr_similar_issue.get_git_provider", raise_unknown)
    assert _provider_supports_issue_indexing() is False
