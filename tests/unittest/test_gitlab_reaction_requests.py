"""Pin the number of API calls the GitLab reaction helpers make.

`add_reaction` and `remove_reaction` know the project, the merge request and the note up front,
but each used to fetch all three objects first: three GETs whose only purpose was to reach an
emoji endpoint that needs none of them. On a busy instance that is avoidable rate-limit spend on
every acknowledged command.

Adding a reaction needs only its POST. Removing the returned reaction ID needs only its DELETE,
without listing emojis or fetching any parent objects.

The client here is a real `gitlab.Gitlab` with only its transport replaced, so the count is the
count python-gitlab would make on the wire. A test built on a mock of `gl.projects` could not
tell a lazy handle from a fetched one, and would keep passing if the fetches came back.
"""

import json

import gitlab
import pytest
from gitlab import GitlabCreateError, GitlabDeleteError
from requests.exceptions import RequestException

from pr_agent.git_providers.gitlab_provider import GitLabProvider

EMOJI_PATH = "/projects/group%2Frepo/merge_requests/7/notes/99/award_emoji"


class _Response:
    """The parts of `requests.Response` that python-gitlab reads."""

    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.headers = {"Content-Type": "application/json"}
        self.encoding = "utf-8"
        self.text = json.dumps(body)
        self.content = self.text.encode()
        self.reason = "OK"
        self.url = "https://example.invalid"
        self.links = {}  # python-gitlab follows pagination from here

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


def _provider(client):
    """A provider on the given client, pointed at one project and merge request."""
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.gl = client
    provider.id_project = "group/repo"
    provider.id_mr = 7
    return provider


def _provider_with_recorded_calls(award_emojis, error=None):
    """A provider whose client counts requests instead of sending them."""
    calls = []

    def http_request(method, path, **kwargs):
        calls.append((method.upper(), path))
        if error is not None:
            raise error
        if method == "post":
            return _Response(201, {"id": 42, "name": (kwargs.get("post_data") or {}).get("name")})
        if method == "get":
            return _Response(200, award_emojis)
        return _Response(204, None)

    client = gitlab.Gitlab("https://example.invalid", private_token="token")
    client.http_request = http_request
    return _provider(client), calls


def test_add_reaction_posts_the_emoji_without_fetching_the_objects():
    provider, calls = _provider_with_recorded_calls([])

    assert provider.add_reaction(99, "eyes") == 42
    assert calls == [("POST", EMOJI_PATH)]


def test_remove_reaction_deletes_by_id_without_fetching_the_objects_or_listing():
    provider, calls = _provider_with_recorded_calls([])

    assert provider.remove_reaction(99, 42) is True
    assert calls == [("DELETE", f"{EMOJI_PATH}/42")]


def test_remove_reaction_reports_a_missing_id_without_listing():
    provider, calls = _provider_with_recorded_calls(
        [], error=GitlabDeleteError("404 Award Emoji Not Found", response_code=404))

    assert provider.remove_reaction(99, 42) is False
    assert calls == [("DELETE", f"{EMOJI_PATH}/42")]


@pytest.mark.parametrize("error", [GitlabCreateError("404 note not found"),
                                   RequestException("connection reset")])
def test_an_api_error_on_the_emoji_endpoint_is_swallowed(error):
    """Return None for GitLab or transport errors at the only remaining request endpoint."""
    provider, calls = _provider_with_recorded_calls([], error=error)

    assert provider.add_reaction(99, "eyes") is None
    assert calls == [("POST", EMOJI_PATH)], "the failure came from the emoji call, not from a handle"


def test_a_delete_that_fails_is_swallowed():
    """Keep an award-emoji deletion failure cosmetic after reaching it without fetches."""
    calls = []

    def http_request(method, path, **kwargs):
        calls.append(method.upper())
        raise GitlabDeleteError("500 internal error")

    client = gitlab.Gitlab("https://example.invalid", private_token="token")
    client.http_request = http_request

    assert _provider(client).remove_reaction(99, 42) is False
    assert calls == ["DELETE"]


@pytest.mark.parametrize(("method", "reaction"), [("add_reaction", "eyes"), ("remove_reaction", 42)])
def test_a_bug_in_our_own_code_is_not_reported_as_an_api_failure(method, reaction):
    """Propagate unexpected errors from either reaction operation."""
    provider, _ = _provider_with_recorded_calls([], error=TypeError("unhashable type"))

    with pytest.raises(TypeError):
        getattr(provider, method)(99, reaction)


@pytest.mark.parametrize("id_mr", [None, 0])
def test_reactions_need_no_api_call_without_a_merge_request(id_mr):
    provider, calls = _provider_with_recorded_calls([{"id": 42, "name": "eyes"}])
    provider.id_mr = id_mr

    assert provider.add_reaction(99, "eyes") is None
    assert provider.remove_reaction(99, 42) is False
    assert calls == []
