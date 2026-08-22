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


def test_parse_merge_request_url_does_not_strip_projects_from_namespace():
    project, mr_id = _provider()._parse_merge_request_url(
        "https://gitlab.example.com/group/projects/project/-/merge_requests/7"
    )

    assert project == "group/projects/project"
    assert mr_id == 7
