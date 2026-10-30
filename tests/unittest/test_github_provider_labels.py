"""
Tests for GitHub provider label publishing payload shape.

``publish_labels`` PUTs label names to the issue \"Set labels for an issue\" endpoint,
which requires an array of label name strings. These tests pin that payload shape so
an accidental regression back to label objects cannot slip through.
"""

from types import SimpleNamespace

import pytest
from github import GithubException
from requests.exceptions import RequestException

from pr_agent.git_providers.github_provider import GithubProvider


class _FakeRequester:
    """Records REST calls instead of reaching the GitHub API."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = iter(responses) if responses is not None else None

    def requestJsonAndCheck(self, method, url, input=None):
        self.calls.append((method, url, input))
        if self.responses is not None:
            result = next(self.responses)
            if isinstance(result, Exception):
                raise result
            return result
        return ({}, [{"name": "Review effort 3/5"}])


def _make_provider(requester):
    provider = GithubProvider.__new__(GithubProvider)
    provider.pr = SimpleNamespace(
        _requester=requester,
        issue_url="https://api.github.com/repos/owner/repo/issues/42",
    )
    return provider


@pytest.mark.parametrize("labels", [["Review effort 3/5", "Enhancement"], []])
def test_publish_labels_sends_label_names_as_strings(labels):
    """Send an array of label names, not {\"name\", \"color\"} objects.

    The GitHub set-labels endpoint rejects an object payload with a 422 that the
    provider downgrades to a warning, which would leave the labels silently unset.
    """
    requester = _FakeRequester()
    provider = _make_provider(requester)

    assert provider.publish_labels(labels) is True

    assert requester.calls == [
        (
            "PUT",
            "https://api.github.com/repos/owner/repo/issues/42/labels",
            labels,
        )
    ]


@pytest.mark.parametrize("error", [GithubException(422, {"message": "rejected"}, {}), RequestException("network blip")])
def test_publish_labels_reports_known_write_failure(error):
    requester = _FakeRequester([error])

    assert _make_provider(requester).publish_labels(["Bug fix"]) is False
    assert len(requester.calls) == 1


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


@pytest.mark.parametrize("header_name", ["link", "Link", "LiNk"])
def test_refresh_reads_every_label_page(header_name):
    first_url = "https://api.github.com/repos/owner/repo/issues/42/labels"
    next_url = f"{first_url}?page=2"
    requester = _FakeRequester([
        ({header_name: f'<{next_url}>; rel="next"'}, [{"name": f"label-{i}"} for i in range(30)]),
        ({}, [{"name": f"label-{i}"} for i in range(30, 35)]),
    ])

    assert _make_provider(requester).get_pr_labels(update=True) == [f"label-{i}" for i in range(35)]
    assert requester.calls == [("GET", first_url, None), ("GET", next_url, None)]


def test_refresh_discards_partial_labels_if_later_page_fails():
    first_url = "https://api.github.com/repos/owner/repo/issues/42/labels"
    requester = _FakeRequester([
        ({"Link": f'<{first_url}?page=2>; rel="next"'}, [{"name": "existing"}]),
        GithubException(502, {"message": "failed to read next page"}, {}),
    ])

    assert _make_provider(requester).get_pr_labels(update=True) is None


@pytest.mark.parametrize("payload", [None, [None], [{"other": "value"}], {}, "", [{"name": None}]])
def test_refresh_rejects_malformed_label_pages(payload):
    requester = _FakeRequester([({}, payload)])

    assert _make_provider(requester).get_pr_labels(update=True) is None


@pytest.mark.parametrize("payload", [{}, ""])
def test_refresh_rejects_malformed_later_page(payload):
    first_url = "https://api.github.com/repos/owner/repo/issues/42/labels"
    requester = _FakeRequester([
        ({"link": f'<{first_url}?page=2>; rel="next"'}, [{"name": "existing"}]),
        ({}, payload),
    ])

    assert _make_provider(requester).get_pr_labels(update=True) is None


def test_refresh_rejects_repeated_page_url():
    first_url = "https://api.github.com/repos/owner/repo/issues/42/labels"
    requester = _FakeRequester([
        ({"link": f'<{first_url}>; rel="next"'}, [{"name": "existing"}]),
    ])

    assert _make_provider(requester).get_pr_labels(update=True) is None
    assert requester.calls == [("GET", first_url, None)]


@pytest.mark.parametrize("next_url", [
    "https://another.example/repos/owner/repo/issues/42/labels?page=2",
    "http://api.github.com/repos/owner/repo/issues/42/labels?page=2",
])
def test_refresh_rejects_untrusted_pagination_url(next_url):
    first_url = "https://api.github.com/repos/owner/repo/issues/42/labels"
    requester = _FakeRequester([
        ({"link": f'<{next_url}>; rel="next"'}, [{"name": "existing"}]),
    ])

    assert _make_provider(requester).get_pr_labels(update=True) is None
    assert requester.calls == [("GET", first_url, None)]


def test_refresh_accepts_canonical_pagination_path():
    first_url = "https://api.github.com/repos/owner/repo/issues/42/labels"
    canonical_next = "https://api.github.com/repositories/1234/issues/42/labels?page=2"
    requester = _FakeRequester([
        ({"link": f'<{canonical_next}>; rel="next"'}, [{"name": "first"}]),
        ({}, [{"name": "second"}]),
    ])

    assert _make_provider(requester).get_pr_labels(update=True) == ["first", "second"]
    assert requester.calls == [("GET", first_url, None), ("GET", f"{first_url}?page=2", None)]
