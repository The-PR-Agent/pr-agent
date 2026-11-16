"""Pin that a GitLab reaction is removed by the id `add_reaction` handed back.

`GitProvider._remove_start_reaction` passes the value `add_reaction` returned straight to
`remove_reaction`, and that value is the emoji's *id*. Matching on the emoji's *name* instead
could never find it, so the start reaction sat next to the outcome one for the life of the
process. It also cost a listing request on every removal.

The client here is a real `gitlab.Gitlab` with only its transport replaced, so what is asserted
is the endpoint traffic rather than the object graph a mock of `gl.projects` would fake. The
assertions name the emoji endpoint only; parent-resource request counts are covered separately
in test_gitlab_reaction_requests.py.
"""

import json

import gitlab
import pytest
from gitlab import GitlabDeleteError
from requests.exceptions import RequestException

from pr_agent.git_providers.gitlab_provider import GitLabProvider

# matched by suffix, so the path does not depend on whether the project, merge request and note
# were fetched first or reached by a lazy handle; that is a separate change
EMOJI_SUFFIX = "/merge_requests/7/notes/99/award_emoji"


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


def _provider(error=None):
    """Record emoji calls while answering parent-object lookups if they occur.

    Only emoji calls are counted here; test_gitlab_reaction_requests.py checks that lazy
    parent handles avoid those lookups altogether.
    """
    emoji_calls = []

    def http_request(method, path, **kwargs):
        if path.endswith(EMOJI_SUFFIX) or f"{EMOJI_SUFFIX}/" in path:
            if error is not None:
                raise error
            emoji_calls.append((method.upper(), path))
            if method == "post":
                return _Response(201, {"id": 42, "name": (kwargs.get("post_data") or {}).get("name")})
            if method == "get":
                # a regression would list here; an empty list is what makes that fail on the
                # assertion rather than inside python-gitlab on a 204 with no body
                return _Response(200, [])
            return _Response(204, None)
        if "/notes/" in path:
            return _Response(200, {"id": 99})
        if "/merge_requests/" in path:
            return _Response(200, {"id": 7, "iid": 7})
        return _Response(200, {"id": 1, "path_with_namespace": "group/repo"})

    client = gitlab.Gitlab("https://example.invalid", private_token="token")
    client.http_request = http_request
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.gl = client
    provider.id_project = "group/repo"
    provider.id_mr = 7
    return provider, emoji_calls


def test_the_reaction_add_reaction_returns_can_be_taken_down_again():
    """The round trip `GitProvider` relies on: add, then take the start reaction back down."""
    provider, calls = _provider()

    reaction_id = provider.add_reaction(99, "eyes")

    assert reaction_id == 42, "the id is the value remove_reaction is handed"
    assert provider.remove_reaction(99, reaction_id) is True
    assert [method for method, _ in calls] == ["POST", "DELETE"]
    assert calls[-1][1].endswith(f"{EMOJI_SUFFIX}/42"), "the id is what the delete names"


def test_remove_reaction_deletes_by_id_without_listing_first():
    """The id is already in hand, so the emoji list does not have to be fetched to find it."""
    provider, calls = _provider()

    assert provider.remove_reaction(99, 42) is True

    assert [method for method, _ in calls] == ["DELETE"], "one delete, and no listing before it"
    assert calls[0][1].endswith(f"{EMOJI_SUFFIX}/42"), "the id is what the delete names"


@pytest.mark.parametrize("error", [GitlabDeleteError("500 internal error"),
                                   RequestException("connection reset"),
                                   GitlabDeleteError("404 Award Emoji Not Found")])
def test_a_delete_that_fails_is_swallowed(error):
    """Losing a reaction is cosmetic, so a failed removal is logged rather than raised.

    An already-removed reaction answers 404, which lands here too and reads as a False.
    """
    provider, _ = _provider(error=error)

    assert provider.remove_reaction(99, 42) is False


@pytest.mark.parametrize("id_mr", [None, 0])
def test_removing_a_reaction_needs_no_api_call_without_a_merge_request(id_mr):
    provider, calls = _provider()
    provider.id_mr = id_mr

    assert provider.remove_reaction(99, 42) is False
    assert calls == []
