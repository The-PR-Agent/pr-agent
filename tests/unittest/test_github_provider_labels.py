"""
Tests for GitHub provider label publishing payload shape.

``publish_labels`` PUTs label names to the issue \"Set labels for an issue\" endpoint,
which requires an array of label name strings. These tests pin that payload shape so
an accidental regression back to label objects cannot slip through.
"""

from types import SimpleNamespace

from pr_agent.git_providers.github_provider import GithubProvider


class _FakeRequester:
    """Records REST calls instead of reaching the GitHub API."""

    def __init__(self):
        self.calls = []

    def requestJsonAndCheck(self, method, url, input=None):
        self.calls.append((method, url, input))
        return ({}, [{"name": "Review effort 3/5"}])


def _make_provider(requester):
    provider = GithubProvider.__new__(GithubProvider)
    provider.pr = SimpleNamespace(
        _requester=requester,
        issue_url="https://api.github.com/repos/owner/repo/issues/42",
    )
    return provider


def test_publish_labels_sends_label_names_as_strings():
    """Send an array of label names, not {\"name\", \"color\"} objects.

    The GitHub set-labels endpoint rejects an object payload with a 422 that the
    provider downgrades to a warning, which would leave the labels silently unset.
    """
    requester = _FakeRequester()
    provider = _make_provider(requester)

    provider.publish_labels(["Review effort 3/5", "Enhancement"])

    assert requester.calls == [
        (
            "PUT",
            "https://api.github.com/repos/owner/repo/issues/42/labels",
            ["Review effort 3/5", "Enhancement"],
        )
    ]


def test_publish_labels_keeps_model_label_names_untouched():
    """The exact names produced by the review and generate_labels flows are sent."""
    requester = _FakeRequester()
    provider = _make_provider(requester)

    provider.publish_labels(["Bug fix", "Bug fix with tests", "Possible security concern"])

    assert requester.calls[0][2] == [
        "Bug fix",
        "Bug fix with tests",
        "Possible security concern",
    ]
