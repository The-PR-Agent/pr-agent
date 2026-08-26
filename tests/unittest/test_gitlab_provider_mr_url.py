from types import SimpleNamespace

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
