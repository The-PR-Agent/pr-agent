"""Pin the exception contract of the GitLab provider: handle the expected, surface the rest.

Read each test as one method's contract. While these handlers caught bare `Exception`, a
`TypeError` from our own code was indistinguishable from a GitLab outage: it was logged as an
API failure and the run continued with a wrong result. Keep the API or transport error
swallowed the way callers rely on, and let a programming error propagate.

Read the last group as the fallback chain the issue calls the model case: `_project_by_path`
tries three routes, so its terminal warning has to say why all three came up empty.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from gitlab import GitlabError
from requests.exceptions import RequestException

from pr_agent.git_providers.gitlab_provider import GitLabProvider

API_ERRORS = [
    pytest.param(GitlabError("500 internal error"), id="gitlab-api-error"),
    pytest.param(RequestException("connection reset"), id="transport-error"),
]

# A bug in our own code, not a failure of the remote side.
UNEXPECTED_ERRORS = [
    pytest.param(TypeError("unhashable type"), id="TypeError"),
    pytest.param(ValueError("invalid literal"), id="ValueError"),
]


def _make_provider():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.gl = MagicMock()
    provider.id_project = "group/repo"
    provider.id_mr = 7
    provider.mr = MagicMock()
    provider.temp_comments = []
    return provider


@pytest.mark.parametrize("error", API_ERRORS)
def test_add_reaction_returns_none_on_api_failure(error):
    provider = _make_provider()
    provider.gl.projects.get.side_effect = error
    assert provider.add_reaction(11, "eyes") is None


@pytest.mark.parametrize("error", UNEXPECTED_ERRORS)
def test_add_reaction_propagates_unexpected_errors(error):
    provider = _make_provider()
    provider.gl.projects.get.side_effect = error
    with pytest.raises(type(error)):
        provider.add_reaction(11, "eyes")


@pytest.mark.parametrize("error", API_ERRORS)
def test_get_pr_labels_falls_back_to_the_cached_mr_on_api_failure(error):
    provider = _make_provider()
    provider.mr.labels = ["cached"]
    provider._get_merge_request = MagicMock(side_effect=error)
    assert provider.get_pr_labels(update=True) == ["cached"]


@pytest.mark.parametrize("error", UNEXPECTED_ERRORS)
def test_get_pr_labels_propagates_unexpected_errors(error):
    provider = _make_provider()
    provider.mr.labels = ["cached"]
    provider._get_merge_request = MagicMock(side_effect=error)
    with pytest.raises(type(error)):
        provider.get_pr_labels(update=True)


@pytest.mark.parametrize("error", API_ERRORS)
def test_resolve_comment_thread_soft_fails_on_api_failure(error):
    provider = _make_provider()
    provider.mr.discussions.list.side_effect = error
    provider.resolve_comment_thread(SimpleNamespace(id=1))  # must not raise


@pytest.mark.parametrize("error", UNEXPECTED_ERRORS)
def test_resolve_comment_thread_propagates_unexpected_errors(error):
    provider = _make_provider()
    provider.mr.discussions.list.side_effect = error
    with pytest.raises(type(error)):
        provider.resolve_comment_thread(SimpleNamespace(id=1))


# ---------------------------------------------------------------------------
# _project_by_path: three routes, one reason
# ---------------------------------------------------------------------------

def _capture_warnings(monkeypatch):
    warnings = []
    logger = MagicMock()
    logger.warning.side_effect = lambda message, *a, **k: warnings.append(message)
    monkeypatch.setattr("pr_agent.git_providers.gitlab_provider.get_logger", lambda: logger)
    return warnings


def test_project_by_path_reports_why_every_route_failed(monkeypatch):
    warnings = _capture_warnings(monkeypatch)
    provider = _make_provider()
    provider.gl.projects.get.side_effect = GitlabError("404 Project Not Found")
    provider.gl.projects.list.side_effect = RequestException("connection reset")

    assert provider._project_by_path("group/sub") is None

    assert len(warnings) == 1
    message = warnings[0]
    assert "group/sub" in message
    # All three routes are named, so the reader can tell a missing project from a dead network.
    assert "encoded path: 404 Project Not Found" in message
    assert "raw path: 404 Project Not Found" in message
    assert "search: connection reset" in message


def test_project_by_path_reports_a_search_that_matched_the_wrong_project(monkeypatch):
    warnings = _capture_warnings(monkeypatch)
    provider = _make_provider()
    provider.gl.projects.get.side_effect = GitlabError("404 Project Not Found")
    provider.gl.projects.list.return_value = [SimpleNamespace(path_with_namespace="other/sub", id=3)]

    assert provider._project_by_path("group/sub") is None
    assert "search matched 1 project(s), none with this exact path" in warnings[0]


def test_project_by_path_returns_the_project_when_a_route_works():
    provider = _make_provider()
    project = SimpleNamespace(path_with_namespace="group/sub")
    provider.gl.projects.get.return_value = project

    assert provider._project_by_path("group/sub") is project


def test_project_by_path_propagates_unexpected_errors():
    provider = _make_provider()
    provider.gl.projects.get.side_effect = TypeError("bad argument")
    with pytest.raises(TypeError):
        provider._project_by_path("group/sub")
