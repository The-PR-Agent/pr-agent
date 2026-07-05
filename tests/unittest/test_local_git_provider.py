import subprocess

import pytest

from pr_agent.algo.types import EDIT_TYPE
from pr_agent.git_providers.local_git_provider import LocalGitProvider, PullRequestMimic
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

LOCAL_SETTINGS_KEYS = (
    "local.description_path",
    "local.review_path",
    "pr_reviewer.inline_code_comments",
)


def _git(*args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "HOME": str(cwd),  # avoid picking up user-level git config
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


@pytest.fixture
def local_repo(tmp_path):
    """A real git repository with a 'main' branch and a checked-out 'feature' branch.

    The feature branch modifies file.txt, adds added.py and deletes to_delete.txt.
    """
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git("init", "-b", "main", cwd=repo_path)
    (repo_path / "file.txt").write_text("line1\nline2\n")
    (repo_path / "to_delete.txt").write_text("obsolete\n")
    _git("add", ".", cwd=repo_path)
    _git("commit", "-m", "initial commit", cwd=repo_path)

    _git("checkout", "-b", "feature", cwd=repo_path)
    (repo_path / "file.txt").write_text("line1\nline2 modified\n")
    (repo_path / "added.py").write_text("print('hello')\n")
    (repo_path / "to_delete.txt").unlink()
    _git("add", "-A", cwd=repo_path)
    _git("commit", "-m", "feature commit adding, modifying and deleting files", cwd=repo_path)
    return repo_path


@pytest.fixture
def provider(local_repo, monkeypatch):
    """LocalGitProvider bound to the temporary repo, with settings restored afterwards."""
    snapshot = snapshot_settings(LOCAL_SETTINGS_KEYS)
    monkeypatch.setattr(
        "pr_agent.git_providers.local_git_provider._find_repository_root",
        lambda: local_repo,
    )
    yield LocalGitProvider("main")
    restore_settings(snapshot)


class TestPullRequestMimic:
    def test_stores_title_and_diff_files(self):
        mimic = PullRequestMimic("my title", [])
        assert mimic.title == "my title"
        assert mimic.diff_files == []


class TestLocalGitProviderInit:
    def test_dirty_repo_raises_value_error(self, local_repo, monkeypatch):
        snapshot = snapshot_settings(LOCAL_SETTINGS_KEYS)
        try:
            monkeypatch.setattr(
                "pr_agent.git_providers.local_git_provider._find_repository_root",
                lambda: local_repo,
            )
            (local_repo / "file.txt").write_text("uncommitted change\n")
            with pytest.raises(ValueError):
                LocalGitProvider("main")
        finally:
            restore_settings(snapshot)

    def test_missing_target_branch_raises_key_error(self, local_repo, monkeypatch):
        snapshot = snapshot_settings(LOCAL_SETTINGS_KEYS)
        try:
            monkeypatch.setattr(
                "pr_agent.git_providers.local_git_provider._find_repository_root",
                lambda: local_repo,
            )
            with pytest.raises(KeyError):
                LocalGitProvider("no-such-branch")
        finally:
            restore_settings(snapshot)

    def test_missing_repository_root_raises_value_error(self, monkeypatch):
        monkeypatch.setattr(
            "pr_agent.git_providers.local_git_provider._find_repository_root",
            lambda: None,
        )
        with pytest.raises(ValueError):
            LocalGitProvider("main")

    def test_init_builds_pr_mimic_and_default_paths(self, provider, local_repo):
        assert provider.head_branch_name == "feature"
        assert provider.target_branch_name == "main"
        assert provider.pr.title == "feature"
        assert provider.description_path == local_repo / "description.md"
        assert provider.review_path == local_repo / "review.md"

    def test_init_disables_inline_code_comments(self, provider):
        from pr_agent.config_loader import get_settings

        assert get_settings().pr_reviewer.inline_code_comments is False


class TestDiffFiles:
    def test_get_files_lists_changed_paths(self, provider):
        assert set(provider.get_files()) == {"file.txt", "added.py", "to_delete.txt"}

    def test_get_diff_files_edit_types_and_content(self, provider):
        diff_files = provider.get_diff_files()
        by_name = {f.filename or f.old_filename: f for f in diff_files}
        assert set(by_name.keys()) == {"file.txt", "added.py", "to_delete.txt"}

        added = by_name["added.py"]
        assert added.edit_type == EDIT_TYPE.ADDED
        assert added.base_file == ""
        assert added.head_file == "print('hello')\n"

        modified = by_name["file.txt"]
        assert modified.edit_type == EDIT_TYPE.MODIFIED
        assert modified.base_file == "line1\nline2\n"
        assert modified.head_file == "line1\nline2 modified\n"
        assert "+line2 modified" in modified.patch
        assert "-line2" in modified.patch

        deleted = by_name["to_delete.txt"]
        assert deleted.edit_type == EDIT_TYPE.DELETED
        assert deleted.base_file == "obsolete\n"
        assert deleted.head_file == ""


class TestCapabilities:
    @pytest.mark.parametrize("capability", [
        "get_issue_comments",
        "create_inline_comment",
        "publish_inline_comments",
        "get_labels",
        "gfm_markdown",
    ])
    def test_unsupported_capabilities(self, capability):
        provider = LocalGitProvider.__new__(LocalGitProvider)
        assert provider._is_supported(capability) is False

    @pytest.mark.parametrize("capability", ["publish_description", "anything_else"])
    def test_supported_capabilities(self, capability):
        provider = LocalGitProvider.__new__(LocalGitProvider)
        assert provider._is_supported(capability) is True


class TestPublishing:
    def test_publish_description_writes_title_and_body(self, provider):
        provider.publish_description("My PR", "the body")
        assert provider.description_path.read_text() == "My PR\nthe body"

    def test_publish_description_uses_branch_name_when_title_is_none(self, provider):
        provider.publish_description(None, "the body")
        assert provider.description_path.read_text() == "feature\nthe body"

    def test_publish_comment_writes_review_file(self, provider):
        provider.publish_comment("looks good")
        assert provider.review_path.read_text() == "looks good"

    @pytest.mark.parametrize("method, args", [
        ("publish_inline_comment", ("body", "file.py", "line")),
        ("publish_inline_comments", ([{"body": "x"}],)),
        ("publish_code_suggestion", ("body", "file.py", 1, 2)),
        ("publish_code_suggestions", ([],)),
        ("get_issue_comments", ()),
        ("get_pr_labels", ()),
    ])
    def test_unimplemented_methods_raise(self, method, args):
        provider = LocalGitProvider.__new__(LocalGitProvider)
        with pytest.raises(NotImplementedError):
            getattr(provider, method)(*args)


class TestMetadata:
    def test_get_pr_title_is_head_branch_name(self, provider):
        assert provider.get_pr_title() == "feature"

    def test_get_user_id(self, provider):
        assert provider.get_user_id() == -1

    def test_get_pr_description_full_concatenates_commit_messages(self, provider):
        description = provider.get_pr_description_full()
        assert "feature commit" in description
        assert len(description) <= 200

    def test_get_languages_percentages_sum_to_100(self, provider):
        languages = provider.get_languages()
        assert set(languages.keys()) == {"txt", "py"}
        assert sum(languages.values()) == pytest.approx(100.0)
