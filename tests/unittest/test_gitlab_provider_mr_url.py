import json
import time
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
import requests
from gitlab import GitlabAuthenticationError, GitlabGetError, GitlabListError

from pr_agent.git_providers import gitlab_provider
from pr_agent.git_providers.gitlab_provider import GitLabProvider


def _provider(gitlab_url="https://gitlab.example.com"):
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.gitlab_url = gitlab_url
    return provider


def test_parse_merge_request_url_handles_standard_project_path():
    project, mr_id = _provider()._parse_merge_request_url(
        "https://gitlab.example.com/group/project/-/merge_requests/1"
    )

    assert project == "group/project"
    assert mr_id == 1


def test_parse_merge_request_url_handles_nested_project_path():
    project, mr_id = _provider()._parse_merge_request_url(
        "https://gitlab.example.com/group/subgroup/project/-/merge_requests/42"
    )

    assert project == "group/subgroup/project"
    assert mr_id == 42


def test_parse_merge_request_url_handles_numeric_project_id_alias():
    project, mr_id = _provider()._parse_merge_request_url(
        "https://gitlab.example.com/projects/127014/-/merge_requests/30"
    )

    assert project == "127014"
    assert mr_id == 30


def test_parse_merge_request_url_keeps_non_ascii_numeric_project_namespace():
    project, mr_id = _provider()._parse_merge_request_url(
        "https://gitlab.example.com/projects/١٢٣/-/merge_requests/30"
    )

    assert project == "projects/١٢٣"
    assert mr_id == 30


def test_parse_merge_request_url_does_not_strip_projects_from_namespace():
    project, mr_id = _provider()._parse_merge_request_url(
        "https://gitlab.example.com/group/projects/project/-/merge_requests/7"
    )

    assert project == "group/projects/project"
    assert mr_id == 7


def test_get_line_link_uses_canonical_project_url_for_numeric_project_id():
    provider = _provider()
    provider.id_project = "127014"
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/30",
        source_branch="feature/test",
    )

    assert provider.get_line_link("src/app.py", 12, 14) == (
        "https://gitlab.example.com/group/project/-/blob/feature/test/src/app.py"
        "?ref_type=heads#L12-14"
    )


def test_get_line_link_uses_numeric_alias_when_merge_request_url_is_unavailable():
    provider = _provider()
    provider.id_project = "127014"
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.mr = SimpleNamespace(web_url="", source_branch="feature/test")

    assert provider.get_line_link("src/app.py", 12) == (
        "https://gitlab.example.com/projects/127014/-/blob/feature/test/src/app.py"
        "?ref_type=heads#L12"
    )


def test_get_line_link_keeps_standard_project_path():
    provider = _provider()
    provider.id_project = "group/project"
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/1",
        source_branch="feature/test",
    )

    assert provider.get_line_link("src/app.py", 8) == (
        "https://gitlab.example.com/group/project/-/blob/feature/test/src/app.py"
        "?ref_type=heads#L8"
    )


def test_get_canonical_url_parts_uses_numeric_alias_when_merge_request_url_is_unavailable():
    provider = _provider()
    provider.pr_url = "https://gitlab.example.com/projects/127014/-/merge_requests/5"
    provider.id_project = "127014"
    provider.gl = SimpleNamespace(
        url="https://gitlab.example.com",
        projects=SimpleNamespace(get=lambda _: SimpleNamespace(default_branch="main")),
    )
    provider.mr = SimpleNamespace(web_url="")

    assert provider.get_canonical_url_parts(repo_git_url=None, desired_branch=None) == (
        "https://gitlab.example.com/projects/127014/-/blob/main",
        "?ref_type=heads",
    )


def test_get_canonical_url_parts_keeps_standard_project_path():
    provider = _provider()
    provider.pr_url = "https://gitlab.example.com/group/project/-/merge_requests/5"
    provider.id_project = "group/project"
    provider.gl = SimpleNamespace(
        url="https://gitlab.example.com",
        projects=SimpleNamespace(get=lambda _: SimpleNamespace(default_branch="main")),
    )
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5"
    )

    assert provider.get_canonical_url_parts(repo_git_url=None, desired_branch=None) == (
        "https://gitlab.example.com/group/project/-/blob/main",
        "?ref_type=heads",
    )


@pytest.mark.parametrize("branch, expected", [
    # A '#' would otherwise start the URL fragment and swallow the file path and line anchor.
    ("project#456", "project%23456"),
    # A slash is a legal path separator in a branch name and must survive unencoded.
    ("feature/cache", "feature/cache"),
    ("feat/a#b", "feat/a%23b"),
])
def test_get_canonical_url_parts_encodes_branch_names(branch, expected):
    provider = _provider()
    provider.pr_url = "https://gitlab.example.com/group/project/-/merge_requests/5"
    provider.id_project = "group/project"
    provider.gl = SimpleNamespace(
        url="https://gitlab.example.com",
        projects=SimpleNamespace(get=lambda _: SimpleNamespace(default_branch=branch)),
    )
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5"
    )

    assert provider.get_canonical_url_parts(repo_git_url=None, desired_branch=None) == (
        f"https://gitlab.example.com/group/project/-/blob/{expected}",
        "?ref_type=heads",
    )


@pytest.mark.parametrize("branch, expected", [
    ("project#456", "project%23456"),
    ("feature/cache", "feature/cache"),
])
def test_get_line_link_encodes_source_branch(branch, expected):
    provider = _provider()
    provider.id_project = "group/project"
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_branch=branch,
    )

    assert provider.get_line_link("src/app.py", 42) == (
        f"https://gitlab.example.com/group/project/-/blob/{expected}/src/app.py?ref_type=heads#L42"
    )


@pytest.fixture
def gitlab_api(monkeypatch):
    elapsed = 0

    def advance(seconds):
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(time, "sleep", advance)
    monkeypatch.setattr(gitlab_provider, "get_settings", lambda: {
        "GITLAB.URL": "https://gitlab.example.com",
        "GITLAB.PERSONAL_ACCESS_TOKEN": "offline-token",
    })

    def make(visible_after=0, failure=404, diff_failure=None):
        def send(_session, request, **_kwargs):
            nonlocal failure, diff_failure
            path = urlparse(request.url).path
            if path.endswith("/api/v4/projects/group%2Fproject"):
                status, payload = 200, {"id": 41}
            elif path.endswith("/merge_requests/39"):
                status = failure if elapsed < visible_after else 200
                if status != 404:
                    failure = 200
                payload = {"iid": 39, "title": "A new merge request"} if status == 200 else {"message": "unavailable"}
            elif path.endswith("/merge_requests/39/versions"):
                status = diff_failure or 200
                payload = [{"id": 1}] if status == 200 else {"message": "unavailable"}
                diff_failure = None
            else:
                raise AssertionError(f"Unexpected GitLab request: {request.url}")
            response = requests.Response()
            response.status_code = status
            response._content = json.dumps(payload).encode()
            response.headers["Content-Type"] = "application/json"
            response.request = request
            return response

        monkeypatch.setattr(requests.Session, "send", send)
        return GitLabProvider("https://gitlab.example.com/group/project/-/merge_requests/39")

    return make


@pytest.mark.parametrize("visible_after", [0, 7])
def test_set_merge_request_waits_for_a_new_merge_request_the_api_does_not_serve_yet(gitlab_api, visible_after):
    provider = gitlab_api(visible_after=visible_after)

    assert provider.get_title() == "A new merge request"


def test_set_merge_request_raises_when_the_merge_request_stays_missing(gitlab_api):
    with pytest.raises(GitlabGetError) as error:
        gitlab_api(visible_after=8)

    assert error.value.response_code == 404


@pytest.mark.parametrize("status, exception", [(401, GitlabAuthenticationError), (403, GitlabGetError),
                                               (500, GitlabGetError)])
def test_set_merge_request_does_not_retry_other_errors(gitlab_api, status, exception):
    with pytest.raises(exception) as error:
        gitlab_api(visible_after=1, failure=status)

    assert error.value.response_code == status


def test_set_merge_request_does_not_retry_a_missing_diff(gitlab_api):
    with pytest.raises(GitlabListError) as error:
        gitlab_api(diff_failure=404)

    assert error.value.response_code == 404
