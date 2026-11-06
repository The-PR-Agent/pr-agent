import copy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from gitlab.exceptions import GitlabGetError
from starlette_context import request_cycle_context

from pr_agent.algo.types import EDIT_TYPE
from pr_agent.config_loader import global_settings
from pr_agent.git_providers.gitlab_provider import GitLabProvider


def _provider(gitlab_url="https://gitlab.example.com"):
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.gitlab_url = gitlab_url
    provider.id_project = "group/repo"
    return provider


def _reviewed_line_snapshot(provider, *filenames, head_sha="reviewed-head"):
    """Supply the reviewed diff required to construct revision-pinned links."""
    provider.diff_files = [SimpleNamespace(
        filename=filename, old_filename=None, edit_type=EDIT_TYPE.MODIFIED,
    ) for filename in filenames]
    provider._diff_head_sha = head_sha


def test_reset_diff_cache_clears_link_lookup_budget_for_next_command():
    provider = _provider()
    provider.diff_files = []
    provider.git_files = ["src/old.py"]
    provider._merge_request_changes_cache = {"changes": [{"new_path": "src/old.py"}]}
    provider._line_link_source_lookup_attempts = 2
    provider._line_link_source_web_url = ""

    provider.reset_diff_cache_for_command()

    assert provider.diff_files is None
    assert provider.git_files is None
    assert provider._merge_request_changes_cache is None
    assert provider._line_link_source_lookup_attempts == 0
    assert provider._line_link_source_web_url is None
    # This stub has no MR to refresh; it cannot be handed to another tool.
    assert provider._tool_provider_stale is True


@pytest.mark.parametrize("fresh,failed,blocked", [
    (b"[ignore]\nglob = ['existing/**']\n", False, False),
    (b"[ignore]\nglob = ['new/**']\n", False, True),
    (b"[ignore]\nglob = ['existing/**']\n", True, True),
])
def test_reset_rejects_changed_or_unavailable_gitlab_repo_settings(fresh, failed, blocked):
    provider = _provider()
    provider._repo_settings_source_options = {}
    provider._repo_settings_failed = False
    previous = [("local", b"[ignore]\nglob = ['existing/**']\n")]

    def refresh():
        provider._repo_settings_failed = failed
        return [("local", fresh)]

    provider.get_repo_settings = MagicMock(side_effect=refresh)
    with request_cycle_context({"settings": copy.deepcopy(global_settings), "repo_settings": previous}):
        provider.reset_diff_cache_for_command()

    assert provider._gitlab_settings_unverified is blocked
    assert provider.get_repo_settings.call_count == (2 if failed else 1)


def test_reset_detects_missing_initial_policy_snapshot():
    provider = _provider()
    settings = copy.deepcopy(global_settings)
    settings.set("config.use_repo_settings_file", True)
    with request_cycle_context({"settings": settings}):
        provider.reset_diff_cache_for_command()
    assert provider._gitlab_settings_unverified is True


def test_reset_compares_effective_toml_instead_of_comment_and_whitespace_changes():
    provider = _provider()
    provider._repo_settings_source_options = {}
    provider._repo_settings_failed = False
    previous = [("local", b"[ignore]\nglob = ['safe/**']\n")]
    provider.get_repo_settings = MagicMock(
        return_value=[("local", b"# extra comment\n[ignore]\n  glob=['safe/**']\n")]
    )
    with request_cycle_context({"settings": copy.deepcopy(global_settings), "repo_settings": previous}):
        provider.reset_diff_cache_for_command()
    assert provider._gitlab_settings_unverified is False
    provider.get_repo_settings.assert_called_once_with()


@pytest.mark.parametrize("nested_changed,blocked", [(False, False), (True, True)])
def test_reset_verifies_nested_policy_contents_not_tree_tip(nested_changed, blocked):
    provider = _provider()
    provider._repo_settings_source_options = {"config_branch": "config"}
    provider._resolved_config_branch = "first-commit"
    provider._repo_root_settings_branch = "config"
    provider._per_directory_settings_policy_enabled = True
    provider._per_directory_settings_source_branch = "config"
    provider._repo_settings_failed = False
    provider.get_repo_settings = MagicMock(return_value=[("local", b"[ignore]\nglob=['safe/**']\n")])
    provider.gl = MagicMock()
    provider.gl.projects.get.return_value.commits.get.return_value.id = "second-commit"
    provider._nested_settings_equal = MagicMock(return_value=not nested_changed)
    settings = copy.deepcopy(global_settings)
    previous = [("local", b"[ignore]\nglob=['safe/**']\n")]

    with request_cycle_context({"settings": settings, "repo_settings": previous}):
        provider.reset_diff_cache_for_command()

    assert provider._gitlab_settings_unverified is blocked
    provider._nested_settings_equal.assert_called_once_with("first-commit", "second-commit")
    provider.gl.projects.get.return_value.commits.get.assert_called_once_with("config")
    assert provider._resolved_config_branch == "first-commit"


def test_nested_settings_manifest_ignores_non_toml_files():
    provider = _provider()
    provider.id_project = "group/repo"
    provider.gl = MagicMock()
    project = provider.gl.projects.get.return_value
    project.repository_tree.return_value = [
        {"type": "blob", "path": "docs/readme.md", "id": "docs-id"},
        {"type": "blob", "path": "docs/.pr_agent.toml", "id": "policy-id"},
    ]
    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        assert provider._get_nested_settings_manifest("immutable-ref") == {
            "docs/.pr_agent.toml": "policy-id"
        }
    project.repository_tree.assert_called_once_with(
        ref="immutable-ref", recursive=True, page=1, per_page=100
    )


def test_nested_settings_manifest_rejects_unbounded_or_malformed_tree():
    provider = _provider()
    provider.id_project = "group/repo"
    provider.gl = MagicMock()
    project = provider.gl.projects.get.return_value
    project.repository_tree.return_value = [
        {"type": "blob", "path": "docs/.pr_agent.toml", "id": ""},
    ]
    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        with pytest.raises(ValueError, match="blob IDs"):
            provider._get_nested_settings_manifest("immutable-ref")


def test_nested_settings_only_fetches_changed_toml_blobs():
    provider = _provider()
    provider.get_pr_file_paths = MagicMock(return_value=["src/app.py"])
    provider._get_nested_settings_manifest = MagicMock(side_effect=[
        {"src/.pr_agent.toml": "blob-1", "docs/.pr_agent.toml": "same"},
        {"src/.pr_agent.toml": "blob-2", "docs/.pr_agent.toml": "same"},
    ])
    provider.get_repo_settings_contents = MagicMock(side_effect=[
        {"src/.pr_agent.toml": b"[ignore]\nglob=['safe/**']\n"},
        {"src/.pr_agent.toml": b"# edit\n[ignore]\nglob=['safe/**']\n"},
    ])
    assert provider._nested_settings_equal("old", "new") is True
    assert provider.get_repo_settings_contents.call_args_list[0].args == (["src/.pr_agent.toml"], "old")
    assert provider.get_repo_settings_contents.call_args_list[1].args == (["src/.pr_agent.toml"], "new")


def test_next_command_refetches_gitlab_file_inventory_after_reset():
    provider = _provider()
    provider.id_project = "group/project"
    provider.id_mr = 42
    provider.git_files = ["src/old.py"]
    provider._merge_request_changes_cache = {
        "changes": [{"new_path": "src/old.py"}],
        "diff_refs": {"head_sha": "old-head", "base_sha": "old-base"},
        "changes_count": "1",
    }
    latest = {
        "changes": [{"new_path": "src/new.py"}],
        "diff_refs": {"head_sha": "new-head", "base_sha": "new-base"},
        "changes_count": "1",
    }
    provider._collect_merge_request_changes = MagicMock(return_value=latest)
    provider._expand_submodule_changes = MagicMock(side_effect=lambda changes, refs: changes)

    provider.reset_diff_cache_for_command()

    assert provider.get_files() == ["src/new.py"]
    assert provider._get_merge_request_changes()["diff_refs"]["head_sha"] == "new-head"
    provider._collect_merge_request_changes.assert_called_once()
    assert provider._merge_request_changes_cache["diff_refs"] == latest["diff_refs"]


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

    # No immutable diff SHA: a mutable branch can advance before the link is read.
    assert provider.get_line_link("src/app.py", 12, 14) == provider.mr.web_url


def test_pre_diff_link_does_not_use_unverified_refs_after_a_push():
    provider = _provider()
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/repo/-/merge_requests/1",
        source_project_id=1,
        target_project_id=1,
        diff_refs={"base_sha": "base", "head_sha": "superseded-head"},
    )
    # Rendering can start after a push but before this command fetches the new
    # diff. Neither the old MR object nor another API read proves a reviewed
    # snapshot exists, so a file-specific SHA would be misleading.
    provider._get_merge_request = MagicMock(return_value=SimpleNamespace(
        diff_refs={"base_sha": "base", "head_sha": "pushed-head"}
    ))

    assert provider.get_line_link("src/app.py", 7) == provider.mr.web_url
    provider._get_merge_request.assert_not_called()


def test_get_line_link_uses_numeric_alias_when_merge_request_url_is_unavailable():
    provider = _provider()
    provider.id_project = "127014"
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.pr_url = "https://gitlab.example.com/projects/127014/-/merge_requests/30"
    provider.mr = SimpleNamespace(web_url="", source_branch="feature/test", source_project_id=1, target_project_id=1)

    assert provider.get_line_link("src/app.py", 12) == provider.pr_url


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

    assert provider.get_line_link("src/app.py", 8) == provider.mr.web_url


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
def test_get_line_link_without_sha_uses_safe_mr_url_for_any_source_branch(branch, expected):
    provider = _provider()
    provider.id_project = "group/project"
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_branch=branch,
        source_project_id=1,
        target_project_id=1,
    )

    assert provider.get_line_link("src/app.py", 42) == provider.mr.web_url


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
    _reviewed_line_snapshot(provider, "src/app.py")
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
    _reviewed_line_snapshot(provider, "src/app.py", "src/other.py")
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
    _reviewed_line_snapshot(provider, "src/app.py", "src/other.py", head_sha="head")
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
    _reviewed_line_snapshot(provider, "src/app.py", head_sha="head")
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


@pytest.mark.parametrize("gitlab_url, repo_git_url, expected", [
    ("https://gitlab.com", "https://gitlab.com/group/docs.git", "https://gitlab.com/group/docs/-/blob/main"),
    (
        "https://gitlab.example.org",
        "https://gitlab.example.org/group/sub/docs.git",
        "https://gitlab.example.org/group/sub/docs/-/blob/main",
    ),
    (
        "https://gitlab.com",
        "https://gitlab.com/group/group.gitlab.io.git",
        "https://gitlab.com/group/group.gitlab.io/-/blob/main",
    ),
    (
        "https://example.org/gitlab",
        "https://example.org/gitlab/group/docs.git",
        "https://example.org/gitlab/group/docs/-/blob/main",
    ),
    (
        "https://gitlab.example.org",
        "https://oauth2:secret@gitlab.example.org/group/docs.git",
        "https://gitlab.example.org/group/docs/-/blob/main",
    ),
])
def test_get_canonical_url_parts_builds_prefix_from_explicit_repo_url(gitlab_url, repo_git_url, expected):
    assert _provider(gitlab_url).get_canonical_url_parts(repo_git_url=repo_git_url, desired_branch="main") == (
        expected,
        "?ref_type=heads",
    )


def test_get_canonical_url_parts_rejects_repo_url_without_host():
    assert _provider().get_canonical_url_parts(
        repo_git_url="git@gitlab.example.com:group/docs.git", desired_branch="main"
    ) == ("", "")
def test_gitlab_uncached_verification_reads_live_global_policy():
    provider = _provider()
    provider.id_project = "team/project"
    provider.gl = MagicMock()
    provider.get_owning_namespace = MagicMock(return_value="team")
    provider._get_global_repo_settings = MagicMock(return_value=b"[ignore]\nglob=['old/**']\n")
    provider._fetch_global_repo_settings = MagicMock(return_value=b"[ignore]\nglob=['new/**']\n")
    provider._get_group_repo_settings = MagicMock(return_value="")
    provider._project_default_branch = MagicMock(return_value="main")
    provider.gl.projects.get.return_value.files.get.side_effect = GitlabGetError(response_code=404)
    settings = copy.deepcopy(global_settings)
    settings.set("config.use_global_settings_file", True)
    settings.set("config.global_settings_repo", "policies")
    with request_cycle_context({"settings": settings}):
        loaded = provider.get_repo_settings()

    assert loaded == [("global", b"[ignore]\nglob=['new/**']\n")]
    provider._fetch_global_repo_settings.assert_called_once_with("team", "policies")
    provider._get_global_repo_settings.assert_not_called()
    provider._get_group_repo_settings.assert_called_once_with(verify_fresh=True)


def test_gitlab_rootless_nested_config_preserves_postmerge_branch_source():
    provider = _provider()
    provider.id_project = "team/project"
    provider.gl = MagicMock()
    provider._resolved_config_branch = ""
    provider._repo_settings_failed = False
    provider._project_default_branch = MagicMock(return_value="main")
    project = provider.gl.projects.get.return_value
    project.commits.get.return_value.id = "pinned-sha"
    project.files.get.side_effect = GitlabGetError(response_code=404)
    provider._get_config_tree_paths = MagicMock(return_value=["docs/.pr_agent.toml"])

    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        paths, revision = provider.get_repo_settings_tree("policy-from-global")

    assert paths == ["docs/.pr_agent.toml"]
    assert revision == "pinned-sha"
    assert provider._per_directory_settings_source_branch == "policy-from-global"
    project.commits.get.assert_called_once_with("policy-from-global")


def test_gitlab_reset_uses_postmerge_nested_source_instead_of_original_root_option():
    provider = _provider()
    provider._repo_settings_source_options = {"config_branch": "host-branch"}
    provider._resolved_config_branch = "pinned-old"
    provider._per_directory_settings_source_branch = "policy-from-global"
    provider._per_directory_settings_policy_enabled = True
    provider._repo_settings_failed = False
    provider.get_repo_settings = MagicMock(return_value=[("global", b"[ignore]\nglob=[]\n")])
    provider.gl = MagicMock()
    provider.gl.projects.get.return_value.commits.get.return_value.id = "pinned-new"
    provider.gl.projects.get.return_value.files.get.side_effect = GitlabGetError(response_code=404)
    provider._nested_settings_equal = MagicMock(return_value=True)
    with request_cycle_context({
        "settings": copy.deepcopy(global_settings),
        "repo_settings": [("global", b"[ignore]\nglob=[]\n")],
    }):
        provider.reset_diff_cache_for_command()

    assert provider._gitlab_settings_unverified is False
    provider.gl.projects.get.return_value.commits.get.assert_called_once_with("policy-from-global")


def test_gitlab_reset_fails_on_malformed_nested_manifest_before_dispatch():
    provider = _provider()
    provider._repo_settings_source_options = {}
    provider._resolved_config_branch = "old"
    provider._repo_root_settings_branch = "policy"
    provider._per_directory_settings_policy_enabled = True
    provider._per_directory_settings_source_branch = "policy"
    provider._repo_settings_failed = False
    provider.get_repo_settings = MagicMock(return_value=[("local", b"[ignore]\nglob=[]\n")])
    provider.gl = MagicMock()
    provider.gl.projects.get.return_value.commits.get.return_value.id = "new"
    provider._nested_settings_equal = MagicMock(side_effect=ValueError("bad tree"))
    with request_cycle_context({
        "settings": copy.deepcopy(global_settings),
        "repo_settings": [("local", b"[ignore]\nglob=[]\n")],
    }):
        provider.reset_diff_cache_for_command()
    assert provider._gitlab_settings_unverified is True


def test_gitlab_malformed_policy_comparison_is_stable_but_detects_raw_changes():
    same = [("local", b"[ignore\nglob=['x/**']")]
    assert GitLabProvider._settings_files_equal(same, list(same)) is True
    assert GitLabProvider._settings_files_equal(
        same, [("local", b"[ignore\nglob=['y/**']")]
    ) is False


def test_gitlab_nested_verification_bounds_changed_file_count():
    provider = _provider()
    provider.get_pr_file_paths = MagicMock(return_value=[f"dir{i}/app.py" for i in range(21)])
    provider._get_nested_settings_manifest = MagicMock(side_effect=[
        {f"dir{i}/.pr_agent.toml": f"old-{i}" for i in range(21)},
        {f"dir{i}/.pr_agent.toml": f"new-{i}" for i in range(21)},
    ])
    provider.get_repo_settings_contents = MagicMock()
    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        with pytest.raises(ValueError, match="Too many changed"):
            provider._nested_settings_equal("old", "new")
    provider.get_repo_settings_contents.assert_not_called()


def test_gitlab_nested_verification_rejects_large_policy_payload():
    provider = _provider()
    provider.get_pr_file_paths = MagicMock(return_value=["dir/app.py"])
    provider._get_nested_settings_manifest = MagicMock(side_effect=[
        {"dir/.pr_agent.toml": "old"},
        {"dir/.pr_agent.toml": "new"},
    ])
    provider.get_repo_settings_contents = MagicMock(side_effect=[
        {"dir/.pr_agent.toml": b"x" * (5 * 1024 * 1024 + 1)}
    ])
    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        with pytest.raises(ValueError, match="exceeds byte limit"):
            provider._nested_settings_equal("old", "new")
    provider.get_repo_settings_contents.assert_called_once_with(
        ["dir/.pr_agent.toml"], "old"
    )


def test_gitlab_nested_verification_rejects_missing_changed_file_payload():
    provider = _provider()
    provider.get_pr_file_paths = MagicMock(return_value=["dir/app.py"])
    provider._get_nested_settings_manifest = MagicMock(side_effect=[
        {"dir/.pr_agent.toml": "old"},
        {"dir/.pr_agent.toml": "new"},
    ])
    provider.get_repo_settings_contents = MagicMock(return_value={})
    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        with pytest.raises(ValueError, match="Missing GitLab nested settings payload"):
            provider._nested_settings_equal("old", "new")


def test_gitlab_detects_new_root_settings_on_postglobal_nested_branch():
    provider = _provider()
    provider._repo_settings_source_options = {"config_branch": "host-branch"}
    provider._resolved_config_branch = "original-tree"
    provider._per_directory_settings_source_branch = "global-policy-branch"
    provider._per_directory_settings_policy_enabled = True
    provider._repo_settings_failed = False
    provider.get_repo_settings = MagicMock(return_value=[("global", b"[ignore]\nglob=[]\n")])
    provider.gl = MagicMock()
    project = provider.gl.projects.get.return_value
    project.commits.get.return_value.id = "updated-tree"
    project.files.get.return_value = MagicMock()
    provider._nested_settings_equal = MagicMock(return_value=True)

    with request_cycle_context({
        "settings": copy.deepcopy(global_settings),
        "repo_settings": [("global", b"[ignore]\nglob=[]\n")],
    }):
        provider.reset_diff_cache_for_command()

    assert provider._gitlab_settings_unverified is True
    project.files.get.assert_called_with(file_path=".pr_agent.toml", ref="updated-tree")
    provider._nested_settings_equal.assert_not_called()


def test_gitlab_nested_verification_ignores_changes_outside_reviewed_directories():
    provider = _provider()
    provider._repo_settings_failed = False
    provider._per_directory_changed_paths = ("src/app.py",)
    provider.get_pr_file_paths = MagicMock(return_value=["src/new.py"])
    provider._get_nested_settings_manifest = MagicMock(side_effect=[
        {"unrelated/.pr_agent.toml": "old", "src/.pr_agent.toml": "same"},
        {"unrelated/.pr_agent.toml": "new", "src/.pr_agent.toml": "same"},
    ])
    provider.get_repo_settings_contents = MagicMock()
    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        assert provider._nested_settings_equal("old-ref", "new-ref") is True

    provider.get_repo_settings_contents.assert_not_called()
    provider.get_pr_file_paths.assert_called_once()


def test_gitlab_nested_verification_includes_newly_touched_directories():
    provider = _provider()
    provider._repo_settings_failed = False
    provider._per_directory_changed_paths = ("src/app.py",)
    provider.get_pr_file_paths = MagicMock(return_value=["docs/new.md"])
    provider._get_nested_settings_manifest = MagicMock(side_effect=[
        {"docs/.pr_agent.toml": "old"},
        {"docs/.pr_agent.toml": "new"},
    ])
    provider.get_repo_settings_contents = MagicMock(side_effect=[
        {"docs/.pr_agent.toml": b"[ignore]\nglob=[]\n"},
        {"docs/.pr_agent.toml": b"[ignore]\nglob=['docs/**']\n"},
    ])
    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        assert provider._nested_settings_equal("old-ref", "new-ref") is False

    assert provider.get_repo_settings_contents.call_count == 2


def test_new_empty_nested_toml_does_not_change_effective_policy():
    provider = _provider()
    provider.get_pr_file_paths = MagicMock(return_value=["docs/app.py"])
    provider._get_nested_settings_manifest = MagicMock(side_effect=[
        {},
        {"docs/.pr_agent.toml": "new-blob"},
    ])
    provider.get_repo_settings_contents = MagicMock(return_value={"docs/.pr_agent.toml": b""})
    settings = copy.deepcopy(global_settings)
    settings.set("config.per_directory_settings_max_files", "20")

    with request_cycle_context({"settings": settings}):
        assert provider._nested_settings_equal("old", "new") is True

    provider.get_repo_settings_contents.assert_called_once_with(["docs/.pr_agent.toml"], "new")


def test_gitlab_nested_verification_budgets_old_and_new_revisions_separately():
    provider = _provider()
    provider.get_pr_file_paths = MagicMock(return_value=["docs/app.py"])
    provider._get_nested_settings_manifest = MagicMock(side_effect=[
        {"docs/.pr_agent.toml": "old-blob"},
        {"docs/.pr_agent.toml": "new-blob"},
    ])
    old_contents = b"[ignore]\nglob=[]\n#" + b"a" * (3 * 1024 * 1024)
    new_contents = b"[ignore]\nglob=[]\n#" + b"b" * (3 * 1024 * 1024)
    provider.get_repo_settings_contents = MagicMock(side_effect=[
        {"docs/.pr_agent.toml": old_contents},
        {"docs/.pr_agent.toml": new_contents},
    ])

    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        assert provider._nested_settings_equal("old", "new") is True

    assert provider.get_repo_settings_contents.call_count == 2


def test_gitlab_nested_verification_ignores_new_unrelated_tree_when_original_had_none():
    provider = _provider()
    provider.get_pr_file_paths = MagicMock(return_value=["src/app.py"])
    provider._get_nested_settings_manifest = MagicMock(side_effect=[
        {},
        {"unrelated/.pr_agent.toml": "new-blob"},
    ])
    provider.get_repo_settings_contents = MagicMock()
    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        assert provider._nested_settings_equal("old", "new") is True
    provider.get_repo_settings_contents.assert_not_called()
    provider.get_pr_file_paths.assert_called_once()


@pytest.mark.parametrize("requested", ["/src/deleted.py", "//SRC/DELETED.PY"])
def test_gitlab_deleted_line_link_normalizes_path_and_uses_base_snapshot(requested):
    provider = _provider()
    provider.id_project = "group/project"
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_project_id=9, target_project_id=1,
    )
    provider.diff_files = [SimpleNamespace(
        filename="src/deleted.py", old_filename="src/old-deleted.py",
        edit_type=EDIT_TYPE.DELETED,
    )]
    provider._diff_head_sha = "reviewed-head"
    provider._diff_base_sha = "reviewed-base"

    assert provider.get_line_link(requested, 7) == (
        "https://gitlab.example.com/group/project/-/blob/"
        "reviewed-base/src/old-deleted.py#L7"
    )
    assert provider.get_line_link("/not-in-diff.py", 7) == provider.mr.web_url


def test_gitlab_snapshot_verification_rejects_oversized_toml_before_parsing():
    from unittest.mock import patch

    original = [("local", b"[ignore]\nglob=[]\n")]
    with patch("pr_agent.git_providers.gitlab_provider.MAX_TOML_SIZE_IN_BYTES", 48):
        with pytest.raises(ValueError, match="byte limit"):
            GitLabProvider._settings_files_equal(original, [("local", b"x" * 49)])
        with pytest.raises(ValueError, match="byte limit"):
            GitLabProvider._settings_files_equal([("local", "é" * 30)], original)


def test_gitlab_live_global_policy_refuses_unresolved_numeric_namespace():
    provider = _provider()
    provider.id_project = "18273"
    provider.gl = MagicMock()
    provider.gl.projects.get.side_effect = GitlabGetError(response_code=503)
    provider._fetch_global_repo_settings = MagicMock()
    settings = copy.deepcopy(global_settings)
    settings.set("config.use_global_settings_file", True)
    settings.set("config.global_settings_repo", "team-policy")

    with request_cycle_context({"settings": settings}):
        assert provider.get_repo_settings() == ""

    assert provider._repo_settings_failed is True
    provider._fetch_global_repo_settings.assert_not_called()


def test_gitlab_line_links_prefer_exact_filename_over_case_collision():
    provider = _provider()
    provider.id_project = "group/project"
    provider.gl = SimpleNamespace(url="https://gitlab.example.com")
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_project_id=1, target_project_id=1,
    )
    provider.diff_files = [
        SimpleNamespace(filename="A.py", old_filename=None, edit_type=EDIT_TYPE.DELETED),
        SimpleNamespace(filename="a.py", old_filename=None, edit_type=EDIT_TYPE.MODIFIED),
    ]
    provider._diff_base_sha = "reviewed-base"
    provider._diff_head_sha = "reviewed-head"

    assert provider.get_line_link("a.py", 7) == (
        "https://gitlab.example.com/group/project/-/blob/reviewed-head/a.py#L7"
    )
    assert provider.get_line_link("A.py", 7) == (
        "https://gitlab.example.com/group/project/-/blob/reviewed-base/A.py#L7"
    )
    assert provider.get_line_link("/a.PY", 7) == provider.mr.web_url


def test_transient_gitlab_policy_verification_failure_does_not_poison_next_command():
    provider = _provider()
    provider._repo_settings_source_options = {}
    provider._repo_settings_failed = False
    previous = [("local", b"[ignore]\nglob=[]\n")]
    status = {"available": False}

    def refresh():
        if not status["available"]:
            raise OSError("GitLab temporarily unavailable")
        return previous

    provider.get_repo_settings = MagicMock(side_effect=refresh)
    with request_cycle_context({"settings": copy.deepcopy(global_settings), "repo_settings": previous}):
        provider.reset_diff_cache_for_command()
        assert provider._gitlab_settings_unverified is True
        assert provider._repo_settings_failed is False

        status["available"] = True
        provider.reset_diff_cache_for_command()
        assert provider._gitlab_settings_unverified is False
        assert provider._repo_settings_failed is False
    assert provider.get_repo_settings.call_count == 3


def test_copied_owner_marker_cannot_assert_untracked_draft_is_stale():
    provider = _provider()
    refs = {"base_sha": "base", "start_sha": "start", "head_sha": "head"}
    owned = SimpleNamespace(id=17, position=refs, note="our draft")
    copied = SimpleNamespace(
        id=99,
        position={**refs, "head_sha": "old"},
        note="human copy\n<!-- pr-agent:gitlab-draft -->",
    )
    provider._gitlab_pending_draft_refs = refs
    provider._gitlab_pending_drafts = [owned]
    provider._get_merge_request = MagicMock(return_value=SimpleNamespace(
        diff_refs=refs, sha="head",
    ))
    # The foreign draft is unknown, not an authenticated stale agent draft.
    assert provider._draft_revision_is_current([copied, owned]) is None
    assert provider._draft_revision_is_current([owned]) is True

@pytest.mark.parametrize("broken_project", [None, SimpleNamespace()])
def test_gitlab_fork_lookup_missing_web_url_returns_mr_page(broken_project):
    provider = _provider()
    project_get = MagicMock(return_value=broken_project)
    provider.gl = SimpleNamespace(
        url="https://gitlab.example.com",
        projects=SimpleNamespace(get=project_get),
    )
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_project_id=99,
        target_project_id=1,
        diff_refs={"head_sha": "reviewed-head"},
    )

    _reviewed_line_snapshot(provider, "src/app.py")
    assert provider.get_line_link("src/app.py", 7) == provider.mr.web_url
    project_get.assert_called_once_with(99)


def test_gitlab_fork_lookup_type_error_returns_mr_page():
    provider = _provider()
    provider.gl = SimpleNamespace(
        url="https://gitlab.example.com",
        projects=SimpleNamespace(get=MagicMock(side_effect=TypeError("malformed source project"))),
    )
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example.com/group/project/-/merge_requests/5",
        source_project_id=99,
        target_project_id=1,
        diff_refs={"head_sha": "reviewed-head"},
    )

    _reviewed_line_snapshot(provider, "src/app.py")
    assert provider.get_line_link("src/app.py", 7) == provider.mr.web_url
