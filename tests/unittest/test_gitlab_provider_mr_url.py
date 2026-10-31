from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from gitlab.exceptions import GitlabGetError

from pr_agent.algo.types import EDIT_TYPE
from pr_agent.git_providers.gitlab_provider import GitLabProvider


def _provider(gitlab_url="https://gitlab.example.com"):
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.gitlab_url = gitlab_url
    return provider


def test_reset_diff_cache_clears_link_lookup_budget_for_next_command():
    provider = _provider()
    provider.diff_files = []
    provider._line_link_source_lookup_attempts = 2
    provider._line_link_source_web_url = ""

    provider.reset_diff_cache_for_command()

    assert provider.diff_files is None
    assert provider._line_link_source_lookup_attempts == 0
    assert provider._line_link_source_web_url is None


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
        source_project_id=1,
        target_project_id=1,
    )

    assert provider.get_line_link("src/app.py", 12, 14) == (
        "https://gitlab.example.com/group/project/-/blob/feature/test/src/app.py"
        "?ref_type=heads#L12-14"
    )


def test_get_line_link_uses_numeric_alias_when_merge_request_url_is_unavailable():
    provider = _provider()
    provider.id_project = "127014"
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.mr = SimpleNamespace(web_url="", source_branch="feature/test", source_project_id=1, target_project_id=1)

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
        source_project_id=1,
        target_project_id=1,
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
        source_project_id=1,
        target_project_id=1,
    )

    assert provider.get_line_link("src/app.py", 42) == (
        f"https://gitlab.example.com/group/project/-/blob/{expected}/src/app.py?ref_type=heads#L42"
    )


def test_get_line_link_pins_fork_file_to_reviewed_source_commit():
    provider = _provider()
    provider.id_project = "group/project"
    source_project = SimpleNamespace(web_url="https://gitlab.example.com/contributor/project")
    get_project = MagicMock(return_value=source_project)
    provider.gl = SimpleNamespace(
        url="https://gitlab.example.com",
        projects=SimpleNamespace(get=get_project),
    )
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_branch="feature",
        source_project_id=99,
        target_project_id=1,
        diff_refs={"base_sha": "old-base", "head_sha": "newer-head"},
    )
    provider.diff_files = [SimpleNamespace(
        filename="src/app.py", old_filename=None, edit_type=EDIT_TYPE.MODIFIED,
    )]
    provider._diff_head_sha = "reviewed-head"
    provider._diff_base_sha = "reviewed-base"

    assert provider.get_line_link("src/app.py", 10, 12) == (
        "https://gitlab.example.com/contributor/project/-/blob/reviewed-head/src/app.py#L10-12"
    )
    get_project.assert_called_once_with(99)


@pytest.mark.parametrize("source_project_id", [99, None])
def test_get_line_link_pins_deleted_file_to_reviewed_target_base(source_project_id):
    provider = _provider()
    provider.id_project = "group/project"
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_branch="feature",
        source_project_id=source_project_id,
        target_project_id=1,
    )
    provider.diff_files = [SimpleNamespace(
        filename="src/deleted.py", old_filename=None, edit_type=EDIT_TYPE.DELETED,
    )]
    provider._diff_head_sha = "reviewed-head"
    provider._diff_base_sha = "reviewed-base"

    assert provider.get_line_link("src/deleted.py", 4) == (
        "https://gitlab.example.com/group/project/-/blob/reviewed-base/src/deleted.py#L4"
    )


def test_get_line_link_fork_without_snapshot_sha_uses_merge_request():
    provider = _provider()
    provider.id_project = "group/project"
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_branch="feature",
        source_project_id=99,
        target_project_id=1,
        diff_refs={},
    )

    assert provider.get_line_link("src/app.py", 4) == provider.mr.web_url


def test_get_line_link_uses_source_repo_for_incremental_fork_deletions():
    provider = _provider()
    provider.id_project = "group/project"
    get_project = MagicMock(return_value=SimpleNamespace(web_url="https://gitlab.example.com/fork/project"))
    provider.gl = SimpleNamespace(
        url="https://gitlab.example.com",
        projects=SimpleNamespace(get=get_project),
    )
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_branch="feature",
        source_project_id=99,
        target_project_id=1,
    )
    provider.diff_files = [SimpleNamespace(
        filename="src/deleted.py", old_filename=None, edit_type=EDIT_TYPE.DELETED,
    )]
    provider._diff_base_sha = "prior-source-commit"
    provider._diff_is_incremental = True

    assert provider.get_line_link("src/deleted.py", 4) == (
        "https://gitlab.example.com/fork/project/-/blob/prior-source-commit/src/deleted.py#L4"
    )
    get_project.assert_called_once_with(99)


def test_get_line_link_incremental_deletion_with_missing_source_uses_merge_request():
    provider = _provider()
    provider.id_project = "group/project"
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_project_id=None,
        target_project_id=1,
    )
    provider.diff_files = [SimpleNamespace(
        filename="src/deleted.py", old_filename=None, edit_type=EDIT_TYPE.DELETED,
    )]
    provider._diff_base_sha = "prior-source-commit"
    provider._diff_is_incremental = True

    assert provider.get_line_link("src/deleted.py", 4) == provider.mr.web_url


def test_get_line_link_with_missing_fork_identity_uses_merge_request():
    provider = _provider()
    provider.id_project = "group/project"
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_branch="feature",
        source_project_id=None,
        target_project_id=1,
        diff_refs={"head_sha": "source-only-sha"},
    )
    assert provider.get_line_link("src/app.py", 9) == provider.mr.web_url


def test_get_line_link_maps_renamed_old_path_to_new_path():
    provider = _provider()
    provider.id_project = "group/project"
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_branch="feature",
        source_project_id=1,
        target_project_id=1,
    )
    provider.diff_files = [SimpleNamespace(
        filename="src/new.py", old_filename="src/old.py", edit_type=EDIT_TYPE.RENAMED,
    )]
    provider._diff_head_sha = "head-commit"
    assert provider.get_line_link("src/old.py", 7) == (
        "https://gitlab.example.com/group/project/-/blob/head-commit/src/new.py#L7"
    )


def test_get_line_link_prefers_current_path_when_old_name_is_reused():
    provider = _provider()
    provider.id_project = "group/project"
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_branch="feature",
        source_project_id=1,
        target_project_id=1,
    )
    provider.diff_files = [
        SimpleNamespace(filename="src/a.py", old_filename="src/z.py", edit_type=EDIT_TYPE.RENAMED),
        SimpleNamespace(filename="src/z.py", old_filename=None, edit_type=EDIT_TYPE.ADDED),
    ]
    provider._diff_head_sha = "reviewed-head"
    assert provider.get_line_link("src/z.py", 7) == (
        "https://gitlab.example.com/group/project/-/blob/reviewed-head/src/z.py#L7"
    )


def test_get_line_link_missing_branch_and_sha_uses_merge_request():
    provider = _provider()
    provider.id_project = "group/project"
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_branch=None,
        diff_refs={},
    )
    assert provider.get_line_link("src/app.py", 7) == provider.mr.web_url


def test_get_line_link_retries_transient_fork_project_lookup_failure():
    provider = _provider()
    provider.id_project = "group/project"
    project_get = MagicMock(side_effect=[
        GitlabGetError("temporarily unavailable"),
        SimpleNamespace(web_url="https://gitlab.example.com/contributor/project"),
    ])
    provider.gl = SimpleNamespace(
        url="https://gitlab.example.com",
        projects=SimpleNamespace(get=project_get),
    )
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_branch="feature",
        source_project_id=99,
        target_project_id=1,
        diff_refs={"head_sha": "reviewed-head"},
    )
    assert provider.get_line_link("src/app.py", 7) == provider.mr.web_url
    assert provider.get_line_link("src/app.py", 7) == (
        "https://gitlab.example.com/contributor/project/-/blob/reviewed-head/src/app.py#L7"
    )
    assert project_get.call_count == 2


def test_get_line_link_caches_missing_fork_project_after_404():
    provider = _provider()
    provider.id_project = "group/project"
    project_get = MagicMock(side_effect=GitlabGetError("not found", response_code=404))
    provider.gl = SimpleNamespace(
        url="https://gitlab.example.com",
        projects=SimpleNamespace(get=project_get),
    )
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_branch="feature",
        source_project_id=99,
        target_project_id=1,
        diff_refs={"head_sha": "reviewed-head"},
    )
    assert provider.get_line_link("src/app.py", 7) == provider.mr.web_url
    assert provider.get_line_link("src/other.py", 9) == provider.mr.web_url
    project_get.assert_called_once_with(99)


def test_get_line_link_without_sha_or_project_identity_returns_merge_request():
    provider = _provider()
    provider.id_project = "group/project"
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_branch="still-present",
        source_project_id=None,
        target_project_id=1,
        diff_refs={},
    )
    assert provider.get_line_link("src/app.py", 7) == provider.mr.web_url


@pytest.mark.parametrize("response_code", [403, 404])
def test_get_line_link_caches_definitive_source_project_failures(response_code):
    provider = _provider()
    provider.id_project = "group/project"
    project_get = MagicMock(side_effect=GitlabGetError("unavailable", response_code=response_code))
    provider.gl = SimpleNamespace(url="https://gitlab.example.com", projects=SimpleNamespace(get=project_get))
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_branch="feature",
        source_project_id=99, target_project_id=1, diff_refs={"head_sha": "head"},
    )
    assert provider.get_line_link("src/app.py", 1) == provider.mr.web_url
    assert provider.get_line_link("src/other.py", 2) == provider.mr.web_url
    project_get.assert_called_once_with(99)


def test_get_line_link_bounds_transient_source_project_failures():
    provider = _provider()
    provider.id_project = "group/project"
    project_get = MagicMock(side_effect=GitlabGetError("temporarily unavailable", response_code=503))
    provider.gl = SimpleNamespace(url="https://gitlab.example.com", projects=SimpleNamespace(get=project_get))
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_branch="feature",
        source_project_id=99, target_project_id=1, diff_refs={"head_sha": "head"},
    )
    for _ in range(5):
        assert provider.get_line_link("src/app.py", 1) == provider.mr.web_url
    assert project_get.call_count == 2


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
