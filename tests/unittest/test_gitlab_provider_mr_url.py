from types import SimpleNamespace

import pytest

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


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.example.com/grp/my-issues/-/merge_requests/1",
        "https://gitlab.example.com/grp/my-issues/-/issues/3",
        "https://gitlab.example.com/grp/my-issues/issues/3",
    ],
)
def test_git_repo_url_of_a_project_named_like_a_resource(url):
    assert _provider().get_git_repo_url(url) == "https://gitlab.example.com/grp/my-issues.git"


def test_git_repo_url_keeps_the_instance_sub_path():
    provider = _provider("https://example.com/gitlab")

    assert provider.get_git_repo_url(
        "https://example.com/gitlab/grp/merge_requests-bot/-/issues/3"
    ) == "https://example.com/gitlab/grp/merge_requests-bot.git"


def test_parse_merge_request_url_of_a_project_named_merge_requests():
    project, mr_id = _provider()._parse_merge_request_url(
        "https://gitlab.example.com/grp/merge_requests/-/merge_requests/5"
    )

    assert project == "grp/merge_requests"
    assert mr_id == 5


def test_parse_merge_request_url_rejects_an_issue_of_a_project_named_merge_requests():
    with pytest.raises(ValueError):
        _provider()._parse_merge_request_url("https://gitlab.example.com/grp/merge_requests/-/issues/5")


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.example.com/grp/my-issues/merge_requests/42/diffs",
        "https://gitlab.example.com/grp/my-issues/-/merge_requests/42/diffs",
    ],
)
def test_merge_request_url_with_a_tab_suffix(url):
    assert _provider()._parse_merge_request_url(url) == ("grp/my-issues", 42)
    assert _provider().get_git_repo_url(url) == "https://gitlab.example.com/grp/my-issues.git"
