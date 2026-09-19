"""A failure the provider expects is handled; a failure it does not expect surfaces.

While these handlers caught bare `Exception`, the two were indistinguishable: a `TypeError`
from our own code was logged as "the GitHub API failed" and the run continued with a wrong
result. Each test below pins both halves — the API or transport error is still swallowed the
way callers rely on, and a programming error propagates instead of being reported as an API
failure.
"""

from types import SimpleNamespace

import pytest
from github import GithubException
from requests.exceptions import RequestException

from pr_agent.git_providers.github_provider import GithubProvider


class _Requester:
    """Raises the configured error for every request, or returns a canned response."""

    def __init__(self, error=None, response=None):
        self.error = error
        self.response = response or ({}, {"id": 1})

    def requestJsonAndCheck(self, method, url, **kwargs):
        if self.error is not None:
            raise self.error
        return self.response


def _make_provider(requester=None, pr_extra=None):
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "owner/repo"
    provider.base_url = "https://api.github.com"
    provider.pr = SimpleNamespace(
        _requester=requester or _Requester(),
        issue_url="https://api.github.com/repos/owner/repo/issues/1",
        **(pr_extra or {}),
    )
    provider.last_commit_id = SimpleNamespace(sha="deadbeef")
    provider._check_run_ids = {}
    provider._check_runs_in_progress = set()
    provider.github_user_id = ""
    return provider


API_ERRORS = [
    pytest.param(GithubException(500, {"message": "boom"}, {}), id="github-api-error"),
    pytest.param(RequestException("connection reset"), id="transport-error"),
]

# A bug in our own code, not a failure of the remote side.
UNEXPECTED_ERRORS = [
    pytest.param(TypeError("unhashable type"), id="TypeError"),
    pytest.param(AttributeError("'NoneType' object has no attribute 'sha'"), id="AttributeError"),
]


@pytest.mark.parametrize("error", API_ERRORS)
def test_find_existing_check_run_returns_none_on_api_failure(error):
    provider = _make_provider(_Requester(error=error))
    assert provider._find_existing_check_run("PR Agent - Review", "deadbeef") is None


@pytest.mark.parametrize("error", UNEXPECTED_ERRORS)
def test_find_existing_check_run_propagates_unexpected_errors(error):
    provider = _make_provider(_Requester(error=error))
    with pytest.raises(type(error)):
        provider._find_existing_check_run("PR Agent - Review", "deadbeef")


@pytest.mark.parametrize("error", API_ERRORS)
def test_add_reaction_returns_none_on_api_failure(error):
    provider = _make_provider(_Requester(error=error))
    assert provider.add_reaction(123, "eyes") is None


@pytest.mark.parametrize("error", UNEXPECTED_ERRORS)
def test_add_reaction_propagates_unexpected_errors(error):
    provider = _make_provider(_Requester(error=error))
    with pytest.raises(type(error)):
        provider.add_reaction(123, "eyes")


@pytest.mark.parametrize("error", API_ERRORS)
def test_get_pr_labels_returns_empty_list_on_api_failure(error):
    provider = _make_provider(_Requester(error=error))
    assert provider.get_pr_labels(update=True) == []


def test_get_pr_labels_propagates_unexpected_errors():
    """TypeError is deliberately expected here: the labels payload is indexed as ``label["name"]``."""
    provider = _make_provider(_Requester(error=AttributeError("no issue_url")))
    with pytest.raises(AttributeError):
        provider.get_pr_labels(update=True)


@pytest.mark.parametrize("error", API_ERRORS)
def test_get_user_id_falls_back_to_empty_on_api_failure(error):
    provider = _make_provider()
    provider.github_client = SimpleNamespace(get_user=lambda: (_ for _ in ()).throw(error))
    assert provider.get_user_id() == ""


def test_get_user_id_propagates_unexpected_errors():
    provider = _make_provider()
    provider.github_client = SimpleNamespace(
        get_user=lambda: (_ for _ in ()).throw(TypeError("bad client"))
    )
    with pytest.raises(TypeError):
        provider.get_user_id()


def test_get_user_id_still_tolerates_a_login_less_payload():
    """The login key is read straight out of the API payload, so KeyError stays expected."""
    provider = _make_provider()
    provider.github_client = SimpleNamespace(get_user=lambda: SimpleNamespace(raw_data={}))
    assert provider.get_user_id() == ""


@pytest.mark.parametrize("error", API_ERRORS)
def test_get_commit_messages_returns_empty_string_on_api_failure(error):
    provider = _make_provider(pr_extra={"get_commits": lambda: (_ for _ in ()).throw(error)})
    assert provider.get_commit_messages() == ""


def test_get_commit_messages_propagates_unexpected_errors():
    provider = _make_provider(pr_extra={"get_commits": lambda: (_ for _ in ()).throw(TypeError("boom"))})
    with pytest.raises(TypeError):
        provider.get_commit_messages()
