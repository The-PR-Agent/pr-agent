"""File links must point to the actual GitLab/Gitea diff snapshots in fork PRs."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from pr_agent.algo.types import EDIT_TYPE
from pr_agent.git_providers.gitea_provider import GiteaProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider


def _reviewed_files():
    return [
        SimpleNamespace(filename="src/new#file.py", old_filename="src/old.py", edit_type=EDIT_TYPE.RENAMED),
        SimpleNamespace(filename="src/removed.py", old_filename="src/removed.py", edit_type=EDIT_TYPE.DELETED),
    ]


def _gitea_provider():
    provider = GiteaProvider.__new__(GiteaProvider)
    provider.base_url_html = "https://gitea.example"
    provider.owner, provider.repo, provider.pr_number = "target", "project", 9
    provider.logger = MagicMock()
    provider.pr = SimpleNamespace(head=SimpleNamespace(
        sha="new-live-head", ref="feature/new",
        repo=SimpleNamespace(full_name="fork/changed"),
    ))
    provider.get_pr_url = lambda: "https://gitea.example/target/project/pulls/9"
    provider.diff_files = _reviewed_files()
    provider._file_link_snapshot = (
        "review-head", "review-base", "fork/source", "feature/original", None
    )
    return provider


def _gitlab_provider():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.pr_url = "https://gitlab.example/target/project/-/merge_requests/9"
    provider.id_project = "target/project"
    provider.gl = SimpleNamespace(
        url="https://gitlab.example",
        projects=SimpleNamespace(get=MagicMock(
            return_value=SimpleNamespace(web_url="https://gitlab.example/group/sub/fork"),
        )),
    )
    provider.mr = SimpleNamespace(
        web_url=provider.pr_url, source_branch="feature/changed",
        source_project_id=22, target_project_id=11,
    )
    provider.diff_files = _reviewed_files()
    provider._diff_head_sha = "review-head"
    provider._file_link_base_sha = "review-base"
    provider._file_link_incremental = False
    return provider


def test_gitea_rename_uses_reviewed_fork_and_head_after_pr_refresh():
    p = _gitea_provider()
    assert p.get_line_link("src/old.py", 4, 6) == (
        "https://gitea.example/fork/source/src/commit/review-head/src/new%23file.py#L4-L6"
    )


def test_gitea_deleted_file_uses_target_merge_base():
    p = _gitea_provider()
    assert p.get_line_link("src/removed.py", 7) == (
        "https://gitea.example/target/project/src/commit/review-base/src/removed.py#L7"
    )


def test_gitea_deleted_file_without_merge_base_falls_back_to_pr():
    p = _gitea_provider()
    p._file_link_snapshot = ("review-head", "", "fork/source", "feature/original", None)
    assert p.get_line_link("src/removed.py", 7) == p.get_pr_url()


def test_gitea_incremental_deletion_uses_source_revision():
    p = _gitea_provider()
    p._file_link_snapshot = (
        "review-head", "review-base", "fork/source", "feature/original", "last-reviewed"
    )
    assert p.get_line_link("src/removed.py", -1) == (
        "https://gitea.example/fork/source/src/commit/last-reviewed/src/removed.py"
    )


def test_gitea_agit_reviewed_link_uses_target_repo_and_head_sha():
    p = _gitea_provider()
    p._file_link_snapshot = ("review-head", "review-base", None, "refs/pull/9/head", None)
    assert p.get_line_link("src/new#file.py", 5) == (
        "https://gitea.example/target/project/src/commit/review-head/src/new%23file.py#L5"
    )


def test_gitea_reviewed_link_without_source_repo_returns_pr():
    p = _gitea_provider()
    p._file_link_snapshot = ("review-head", "review-base", None, "feature/original", None)
    assert p.get_line_link("src/new#file.py", 5) == p.get_pr_url()


def test_gitea_missing_or_ambiguous_file_falls_back_to_pr():
    p = _gitea_provider()
    assert p.get_line_link("missing.py", 7) == p.get_pr_url()
    p.diff_files.append(SimpleNamespace(
        filename="src/NEW#file.py", old_filename=None, edit_type=EDIT_TYPE.ADDED,
    ))
    assert p.get_line_link("SRC/NEW#FILE.PY", 7) == p.get_pr_url()


def test_gitea_fork_without_diff_does_not_link_to_target_branch():
    p = _gitea_provider()
    p.diff_files, p._file_link_snapshot = None, None
    assert p.get_line_link("src/new#file.py", 7) == p.get_pr_url()


def test_gitlab_fork_link_uses_reviewed_revision_and_subgroup():
    p = _gitlab_provider()
    assert p.get_line_link("src/old.py", 4, 6) == (
        "https://gitlab.example/group/sub/fork/-/blob/review-head/src/new%23file.py#L4-6"
    )
    p.gl.projects.get.assert_called_once_with(22)
    assert p.get_line_link("src/old.py", 8).endswith("#L8")
    p.gl.projects.get.assert_called_once()


def test_gitlab_deleted_file_uses_target_base():
    p = _gitlab_provider()
    assert p.get_line_link("src/removed.py", -1) == (
        "https://gitlab.example/target/project/-/blob/review-base/src/removed.py"
    )
    p.gl.projects.get.assert_not_called()


def test_gitlab_full_review_deletion_does_not_require_a_live_source_fork():
    p = _gitlab_provider()
    p.mr.source_project_id = None
    assert p.get_line_link("src/removed.py", 4) == (
        "https://gitlab.example/target/project/-/blob/review-base/src/removed.py#L4"
    )
    p.gl.projects.get.assert_not_called()


def test_gitlab_source_lookup_retries_one_transient_failure():
    from gitlab.exceptions import GitlabGetError

    p = _gitlab_provider()
    p.gl.projects.get.side_effect = [
        GitlabGetError("unavailable", response_code=503),
        SimpleNamespace(web_url="https://gitlab.example/group/sub/fork"),
    ]
    assert p.get_line_link("src/new#file.py", 8) == p.pr_url
    assert p.get_line_link("src/new#file.py", 8) == (
        "https://gitlab.example/group/sub/fork/-/blob/review-head/src/new%23file.py#L8"
    )
    assert p.gl.projects.get.call_count == 2


def test_gitlab_definitive_missing_fork_is_not_retried():
    from gitlab.exceptions import GitlabGetError

    p = _gitlab_provider()
    p.gl.projects.get.side_effect = GitlabGetError("missing", response_code=404)
    assert p.get_line_link("src/new#file.py", 8) == p.pr_url
    assert p.get_line_link("src/new#file.py", 8) == p.pr_url
    p.gl.projects.get.assert_called_once()


def test_gitlab_incremental_deletion_uses_source_base():
    p = _gitlab_provider()
    p._file_link_incremental = True
    p._file_link_base_sha = "last-reviewed"
    assert p.get_line_link("src/removed.py", 9) == (
        "https://gitlab.example/group/sub/fork/-/blob/last-reviewed/src/removed.py#L9"
    )


def test_gitlab_missing_revision_or_source_returns_mr_page():
    p = _gitlab_provider()
    p._diff_head_sha = None
    assert p.get_line_link("src/new#file.py", 8) == p.pr_url
    p._diff_head_sha = "review-head"
    p.gl.projects.get.return_value.web_url = ""
    assert p.get_line_link("src/new#file.py", 8) == p.pr_url


def test_gitlab_missing_reviewed_file_returns_mr_page():
    p = _gitlab_provider()
    assert p.get_line_link("missing.py", 3) == p.pr_url
    p.diff_files = None
    assert p.get_line_link("src/new#file.py", 8) == p.pr_url
