"""Pin the number of API calls the GitLab reaction helpers make.

`add_reaction` and `remove_reaction` know the project, the merge request and the note up front,
but each used to fetch all three objects first: three GETs whose only purpose was to reach an
emoji endpoint that needs none of them. On a busy instance that is avoidable rate-limit spend on
every acknowledged command.

Measured against `main`, that is four requests down to one for `add_reaction`, five down to two
for a `remove_reaction` that finds the emoji, and four down to one for one that does not.

The client here is a real `gitlab.Gitlab` with only its transport replaced, so the count is the
count python-gitlab would make on the wire. A test built on a mock of `gl.projects` could not
tell a lazy handle from a fetched one, and would keep passing if the fetches came back.
"""

import json
from urllib.parse import urlparse

import gitlab
import pytest
from gitlab import GitlabCreateError, GitlabDeleteError
from requests.exceptions import RequestException

from pr_agent.git_providers.gitlab_provider import GitLabProvider

EMOJI_PATH = "/projects/group%2Frepo/merge_requests/7/notes/99/award_emoji"
# python-gitlab hands http_list an absolute URL with the API prefix; the other calls pass the path
LIST_URL_PATH = f"/api/v4{EMOJI_PATH}"


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


def test_remove_reaction_deletes_the_named_emoji_without_fetching_the_objects():
    """Reaching the emoji takes one list and one DELETE; the three fetches are gone.

    A name is what this pins, and it is not what `_remove_start_reaction` passes - it hands over
    the id `add_reaction` returned, which the name lookup cannot match. Nothing here pins that
    mismatch, and fixing it is a separate change; either way the three fetches stay saved.
    """
    provider, calls = _provider_with_recorded_calls(
        [{"id": 7, "name": "tada"}, {"id": 42, "name": "eyes"}])

    assert provider.remove_reaction(99, "eyes") is True
    assert [method for method, _ in calls] == ["GET", "DELETE"]
    assert urlparse(calls[0][1]).path == LIST_URL_PATH
    assert calls[1][1] == f"{EMOJI_PATH}/42", "the emoji that matched, not the first one listed"


def test_remove_reaction_reports_a_name_that_is_not_there_without_deleting():
    provider, calls = _provider_with_recorded_calls(
        [{"id": 7, "name": "tada"}, {"id": 42, "name": "eyes"}])

    assert provider.remove_reaction(99, "rocket") is False
    assert [method for method, _ in calls] == ["GET"], "nothing to delete, so nothing was deleted"


@pytest.mark.parametrize("error", [GitlabCreateError("404 note not found"),
                                   RequestException("connection reset")])
def test_an_api_error_on_the_emoji_endpoint_is_swallowed(error):
    """The pre-existing narrowing test fires on object construction, which does no I/O any more.

    The one request that is left is the emoji endpoint, so this is where the contract that a
    GitLab or transport error is swallowed has to hold.
    """
    provider, calls = _provider_with_recorded_calls([], error=error)

    assert provider.add_reaction(99, "eyes") is None
    assert calls == [("POST", EMOJI_PATH)], "the failure came from the emoji call, not from a handle"


def test_a_delete_that_fails_after_a_successful_list_is_swallowed():
    """`remove_reaction` has two requests; the second one can fail on its own."""
    calls = []

    def http_request(method, path, **kwargs):
        calls.append(method.upper())
        if method == "get":
            return _Response(200, [{"id": 42, "name": "eyes"}])
        raise GitlabDeleteError("500 internal error")

    client = gitlab.Gitlab("https://example.invalid", private_token="token")
    client.http_request = http_request

    assert _provider(client).remove_reaction(99, "eyes") is False
    assert calls == ["GET", "DELETE"]


def test_a_bug_in_our_own_code_is_not_reported_as_an_api_failure():
    """The emoji endpoint is where the work happens, so a TypeError there must not be swallowed.

    Before `lazy=True` the two object fetches sat in the same `try`, so the narrowing was
    reachable. `tests/unittest/test_gitlab_provider_exception_narrowing.py` still exercises it
    there, but that call no longer performs any I/O.
    """
    provider, _ = _provider_with_recorded_calls(
        [], error=TypeError("unhashable type"))

    with pytest.raises(TypeError):
        provider.add_reaction(99, "eyes")


@pytest.mark.parametrize("id_mr", [None, 0])
def test_reactions_need_no_api_call_without_a_merge_request(id_mr):
    provider, calls = _provider_with_recorded_calls([{"id": 42, "name": "eyes"}])
    provider.id_mr = id_mr

    assert provider.add_reaction(99, "eyes") is None
    assert provider.remove_reaction(99, "eyes") is False
    assert calls == []
