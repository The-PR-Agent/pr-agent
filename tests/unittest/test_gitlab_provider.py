from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from gitlab import Gitlab
from gitlab.exceptions import GitlabGetError
from gitlab.v4.objects import Project, ProjectFile

from pr_agent.git_providers.git_provider import IncrementalPR
from pr_agent.git_providers.gitlab_provider import (
    GitLabProvider,
    _GitlabIncrementalCommit,
    _GitlabIncrementalNote,
    _parse_gitlab_iso_datetime,
)


class TestGitLabProvider:
    """Test suite for GitLab provider functionality."""

    @pytest.fixture
    def mock_gitlab_client(self):
        client = MagicMock()
        return client

    @pytest.fixture
    def mock_project(self):
        project = MagicMock()
        return project

    @pytest.fixture
    def gitlab_provider(self, mock_gitlab_client, mock_project):
        with patch('pr_agent.git_providers.gitlab_provider.gitlab.Gitlab', return_value=mock_gitlab_client), \
             patch('pr_agent.git_providers.gitlab_provider.get_settings') as mock_settings:

            mock_settings.return_value.get.side_effect = lambda key, default=None: {
                "GITLAB.URL": "https://gitlab.com",
                "GITLAB.PERSONAL_ACCESS_TOKEN": "fake_token"
            }.get(key, default)

            mock_gitlab_client.projects.get.return_value = mock_project
            provider = GitLabProvider("https://gitlab.com/test/repo/-/merge_requests/1")
            provider.gl = mock_gitlab_client
            provider.id_project = "test/repo"
            return provider

    def test_get_pr_file_content_success(self, gitlab_provider, mock_project):
        mock_file = MagicMock(ProjectFile)
        mock_file.decode.return_value = "# Changelog\n\n## v1.0.0\n- Initial release"
        mock_project.files.get.return_value = mock_file

        content = gitlab_provider.get_pr_file_content("CHANGELOG.md", "main")

        assert content == "# Changelog\n\n## v1.0.0\n- Initial release"
        mock_project.files.get.assert_called_once_with("CHANGELOG.md", "main")
        mock_file.decode.assert_called_once()

    def test_get_pr_file_content_with_bytes(self, gitlab_provider, mock_project):
        mock_file = MagicMock(ProjectFile)
        mock_file.decode.return_value = b"# Changelog\n\n## v1.0.0\n- Initial release"
        mock_project.files.get.return_value = mock_file

        content = gitlab_provider.get_pr_file_content("CHANGELOG.md", "main")

        assert content == "# Changelog\n\n## v1.0.0\n- Initial release"
        mock_project.files.get.assert_called_once_with("CHANGELOG.md", "main")

    def test_get_pr_file_content_file_not_found(self, gitlab_provider, mock_project):
        mock_project.files.get.side_effect = GitlabGetError("404 Not Found")

        content = gitlab_provider.get_pr_file_content("CHANGELOG.md", "main")

        assert content == ""
        mock_project.files.get.assert_called_once_with("CHANGELOG.md", "main")

    def test_get_pr_file_content_other_exception(self, gitlab_provider, mock_project):
        mock_project.files.get.side_effect = Exception("Network error")

        content = gitlab_provider.get_pr_file_content("CHANGELOG.md", "main")

        assert content == ""

    def test_create_or_update_pr_file_create_new(self, gitlab_provider, mock_project):
        mock_project.files.get.side_effect = GitlabGetError("404 Not Found")
        mock_file = MagicMock()
        mock_project.files.create.return_value = mock_file

        new_content = "# Changelog\n\n## v1.1.0\n- New feature"
        commit_message = "Add CHANGELOG.md"

        gitlab_provider.create_or_update_pr_file(
            "CHANGELOG.md", "feature-branch", new_content, commit_message
        )

        mock_project.files.get.assert_called_once_with("CHANGELOG.md", "feature-branch")
        mock_project.files.create.assert_called_once_with({
            'file_path': 'CHANGELOG.md',
            'branch': 'feature-branch',
            'content': new_content,
            'commit_message': commit_message,
        })

    def test_create_or_update_pr_file_update_existing(self, gitlab_provider, mock_project):
        mock_file = MagicMock(ProjectFile)
        mock_file.content = "# Old changelog content"
        mock_project.files.get.return_value = mock_file

        new_content = "# New changelog content"
        commit_message = "Update CHANGELOG.md"

        gitlab_provider.create_or_update_pr_file(
            "CHANGELOG.md", "feature-branch", new_content, commit_message
        )

        mock_project.files.get.assert_called_once_with("CHANGELOG.md", "feature-branch")
        assert mock_file.content == new_content
        mock_file.save.assert_called_once_with(branch="feature-branch", commit_message=commit_message)
        mock_project.files.create.assert_not_called()

    def test_create_or_update_pr_file_update_exception(self, gitlab_provider, mock_project):
        mock_project.files.get.side_effect = Exception("Network error")

        with pytest.raises(Exception):
            gitlab_provider.create_or_update_pr_file(
                "CHANGELOG.md", "feature-branch", "content", "message"
            )

    def test_has_create_or_update_pr_file_method(self, gitlab_provider):
        assert hasattr(gitlab_provider, "create_or_update_pr_file")
        assert callable(getattr(gitlab_provider, "create_or_update_pr_file"))

    def test_method_signature_compatibility(self, gitlab_provider):
        import inspect

        sig = inspect.signature(gitlab_provider.create_or_update_pr_file)
        params = list(sig.parameters.keys())

        expected_params = ['file_path', 'branch', 'contents', 'message']
        assert params == expected_params

    @pytest.mark.parametrize("content,expected", [
        ("simple text", "simple text"),
        (b"bytes content", "bytes content"),
        ("", ""),
        (b"", ""),
        ("unicode: café", "unicode: café"),
        (b"unicode: caf\xc3\xa9", "unicode: café"),
    ])
    def test_content_encoding_handling(self, gitlab_provider, mock_project, content, expected):
        mock_file = MagicMock(ProjectFile)
        mock_file.decode.return_value = content
        mock_project.files.get.return_value = mock_file

        result = gitlab_provider.get_pr_file_content("test.md", "main")

        assert result == expected

    def test_get_gitmodules_map_parsing(self, gitlab_provider, mock_project):
        gitlab_provider.id_project = "1"
        gitlab_provider.mr = MagicMock()
        gitlab_provider.mr.target_branch = "main"

        file_obj = MagicMock(ProjectFile)
        file_obj.decode.return_value = (
            "[submodule \"libs/a\"]\n"
            "    path = \"libs/a\"\n"
            "    url = \"https://gitlab.com/a.git\"\n"
            "[submodule \"libs/b\"]\n"
            "    path = libs/b\n"
            "    url = git@gitlab.com:b.git\n"
        )
        mock_project.files.get.return_value = file_obj
        gitlab_provider.gl.projects.get.return_value = mock_project

        result = gitlab_provider._get_gitmodules_map()
        assert result == {
            "libs/a": "https://gitlab.com/a.git",
            "libs/b": "git@gitlab.com:b.git",
        }

    def test_project_by_path_requires_exact_match(self, gitlab_provider):
        gitlab_provider.gl.projects.get.reset_mock()
        gitlab_provider.gl.projects.get.side_effect = Exception("not found")
        fake = MagicMock()
        fake.id = "mismatched-project-id"
        fake.path_with_namespace = "other/group/repo"
        gitlab_provider.gl.projects.list.return_value = [fake]

        result = gitlab_provider._project_by_path("group/repo")

        assert result is None
        gitlab_provider.gl.projects.list.assert_called_once()
        list_kwargs = gitlab_provider.gl.projects.list.call_args.kwargs
        assert list_kwargs["search"] == "repo"
        assert list_kwargs["membership"] is True
        assert all(call.args[0] != fake.id for call in gitlab_provider.gl.projects.get.call_args_list)

    def test_compare_submodule_cached(self, gitlab_provider):
        proj = MagicMock()
        proj.repository_compare.return_value = {"diffs": [{"diff": "d"}]}
        with patch.object(gitlab_provider, "_project_by_path", return_value=proj) as m_pbp:
            first = gitlab_provider._compare_submodule("grp/repo", "old", "new")
            second = gitlab_provider._compare_submodule("grp/repo", "old", "new")

        assert first == second == [{"diff": "d"}]
        m_pbp.assert_called_once_with("grp/repo")
        proj.repository_compare.assert_called_once_with("old", "new")

    def test_compare_submodule_cache_hit_skips_project_resolution(self, gitlab_provider):
        cached_diffs = [{"diff": "d"}]
        gitlab_provider._submodule_cache[("grp/repo", "old", "new")] = cached_diffs

        with patch.object(gitlab_provider, "_project_by_path") as m_pbp:
            result = gitlab_provider._compare_submodule("grp/repo", "old", "new")

        assert result == cached_diffs
        m_pbp.assert_not_called()

    def test_parse_merge_request_url_handles_nested_project_paths(self, gitlab_provider):
        project_path, mr_id = gitlab_provider._parse_merge_request_url(
            "https://gitlab.com/group/subgroup/repo/-/merge_requests/123"
        )

        assert project_path == "group/subgroup/repo"
        assert mr_id == 123

    def test_get_line_link_handles_file_and_line_ranges(self, gitlab_provider):
        gitlab_provider.gl.url = "https://gitlab.com"
        gitlab_provider.id_project = "group/repo"
        gitlab_provider.mr = MagicMock()
        gitlab_provider.mr.source_branch = "feature/cache"

        assert gitlab_provider.get_line_link("src/app.py", -1) == (
            "https://gitlab.com/group/repo/-/blob/feature/cache/src/app.py?ref_type=heads"
        )
        assert gitlab_provider.get_line_link("src/app.py", 10) == (
            "https://gitlab.com/group/repo/-/blob/feature/cache/src/app.py?ref_type=heads#L10"
        )
        assert gitlab_provider.get_line_link("src/app.py", 10, 12) == (
            "https://gitlab.com/group/repo/-/blob/feature/cache/src/app.py?ref_type=heads#L10-12"
        )


class TestGitLabIncrementalHelpers:
    """Pure-function tests for the incremental-review helpers."""

    @pytest.mark.parametrize("value,expected", [
        ("2024-05-01T10:00:00.000Z", datetime(2024, 5, 1, 10, 0, 0)),
        ("2024-05-01T12:00:00+02:00", datetime(2024, 5, 1, 10, 0, 0)),
        ("2024-05-01T10:00:00", datetime(2024, 5, 1, 10, 0, 0)),
        (datetime(2024, 5, 1, 10, 0, 0), datetime(2024, 5, 1, 10, 0, 0)),
        (None, None),
        ("not a date", None),
        (12345, None),
    ])
    def test_parse_iso_datetime(self, value, expected):
        assert _parse_gitlab_iso_datetime(value) == expected

    def test_commit_adapter_exposes_pygithub_shape(self):
        gl_commit = MagicMock()
        gl_commit.id = "abc123"
        gl_commit.committed_date = "2024-05-01T10:00:00.000Z"
        gl_commit.authored_date = "2024-04-30T10:00:00.000Z"

        adapter = _GitlabIncrementalCommit(gl_commit)

        assert adapter.sha == "abc123"
        # committed_date takes precedence over authored_date
        assert adapter.commit.author.date == datetime(2024, 5, 1, 10, 0, 0)

    def test_commit_adapter_falls_back_to_authored_date(self):
        gl_commit = MagicMock(spec=["id", "authored_date"])
        gl_commit.id = "abc"
        gl_commit.authored_date = "2024-04-30T10:00:00Z"

        adapter = _GitlabIncrementalCommit(gl_commit)

        assert adapter.commit.author.date == datetime(2024, 4, 30, 10, 0, 0)

    def test_note_adapter_builds_html_url(self):
        note = MagicMock()
        note.id = 42
        note.body = "## PR Reviewer Guide 🔍\n..."
        note.created_at = "2024-05-01T10:00:00Z"

        adapter = _GitlabIncrementalNote(note, mr_web_url="https://gitlab.com/x/y/-/merge_requests/1")

        assert adapter.id == 42
        assert adapter.html_url == "https://gitlab.com/x/y/-/merge_requests/1#note_42"
        assert adapter.created_at == datetime(2024, 5, 1, 10, 0, 0)


class TestGitLabIncrementalReview:
    """Tests for the GitLab incremental-review flow."""

    @pytest.fixture
    def mock_gitlab_client(self):
        return MagicMock()

    @pytest.fixture
    def mock_project(self):
        return MagicMock()

    @pytest.fixture
    def gitlab_provider(self, mock_gitlab_client, mock_project):
        with patch('pr_agent.git_providers.gitlab_provider.gitlab.Gitlab', return_value=mock_gitlab_client), \
             patch('pr_agent.git_providers.gitlab_provider.get_settings') as mock_settings:
            mock_settings.return_value.get.side_effect = lambda key, default=None: {
                "GITLAB.URL": "https://gitlab.com",
                "GITLAB.PERSONAL_ACCESS_TOKEN": "fake_token",
            }.get(key, default)
            mock_gitlab_client.projects.get.return_value = mock_project
            provider = GitLabProvider("https://gitlab.com/test/repo/-/merge_requests/1")
            provider.gl = mock_gitlab_client
            provider.id_project = "test/repo"
            provider.mr = MagicMock()
            provider.mr.web_url = "https://gitlab.com/test/repo/-/merge_requests/1"
            provider.mr.diff_refs = {"base_sha": "base", "head_sha": "head", "start_sha": "base"}
            return provider

    @staticmethod
    def _make_note(note_id, body, created_at):
        n = MagicMock()
        n.id = note_id
        n.body = body
        n.created_at = created_at
        return n

    @staticmethod
    def _make_commit(sha, committed_date):
        c = MagicMock(spec=["id", "committed_date", "authored_date", "created_at"])
        c.id = sha
        c.committed_date = committed_date
        c.authored_date = committed_date
        c.created_at = committed_date
        return c

    def test_get_incremental_commits_no_previous_review_falls_back(self, gitlab_provider):
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(1, "Just a comment", "2024-05-01T10:00:00Z"),
        ]
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c1", "2024-05-02T10:00:00Z"),
        ]

        gitlab_provider.get_incremental_commits(IncrementalPR(True))

        assert gitlab_provider.incremental.is_incremental is False

    def test_get_incremental_commits_picks_commits_after_review(self, gitlab_provider, mock_project):
        # Previous review at T=10:00. Commit c0 at 09:00 (before), c1 and c2 at 11:00 (after).
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(7, "## PR Reviewer Guide 🔍\nbody", "2024-05-01T10:00:00Z"),
            self._make_note(1, "older note", "2024-04-01T10:00:00Z"),
        ]
        # gitlab returns commits newest-first
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c2", "2024-05-01T11:30:00Z"),
            self._make_commit("c1", "2024-05-01T11:00:00Z"),
            self._make_commit("c0", "2024-05-01T09:00:00Z"),
        ]
        mock_project.repository_compare.return_value = {
            "diffs": [
                {"new_path": "a.py", "old_path": "a.py", "diff": "@@ -1 +1 @@\n-old\n+new\n",
                 "new_file": False, "deleted_file": False, "renamed_file": False},
                {"new_path": "b.py", "old_path": "b.py", "diff": "@@ ... @@",
                 "new_file": True, "deleted_file": False, "renamed_file": False},
            ]
        }
        # mr.changes() is intersected with repository_compare to exclude files brought in
        # via a merge from the target branch. Here both files are part of the MR.
        gitlab_provider.mr.changes.return_value = {
            "changes": [{"new_path": "a.py"}, {"new_path": "b.py"}]
        }

        gitlab_provider.get_incremental_commits(IncrementalPR(True))

        assert gitlab_provider.incremental.is_incremental is True
        assert gitlab_provider.incremental.first_new_commit_sha == "c1"
        assert gitlab_provider.incremental.last_seen_commit_sha == "c0"
        assert set(gitlab_provider.unreviewed_files_set.keys()) == {"a.py", "b.py"}
        mock_project.repository_compare.assert_called_once_with("c0", "head")

    def test_get_incremental_commits_no_new_commits_yields_empty_set(self, gitlab_provider, mock_project):
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(7, "## PR Reviewer Guide 🔍\nbody", "2024-05-01T20:00:00Z"),
        ]
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c0", "2024-05-01T09:00:00Z"),
        ]

        gitlab_provider.get_incremental_commits(IncrementalPR(True))

        # is_incremental stays True so the reviewer publishes the "no new files" message;
        # unreviewed_files_set is empty.
        assert gitlab_provider.incremental.is_incremental is True
        assert gitlab_provider.unreviewed_files_set == {}
        mock_project.repository_compare.assert_not_called()

    def test_get_incremental_commits_no_anchor_commit_falls_back(self, gitlab_provider, mock_project):
        # All commits are after the previous review -> no last_seen_commit -> can't anchor.
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(7, "## PR Reviewer Guide 🔍\nbody", "2024-05-01T08:00:00Z"),
        ]
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c1", "2024-05-01T11:00:00Z"),
        ]

        gitlab_provider.get_incremental_commits(IncrementalPR(True))

        assert gitlab_provider.incremental.is_incremental is False
        mock_project.repository_compare.assert_not_called()

    def test_get_files_uses_incremental_set_when_active(self, gitlab_provider):
        gitlab_provider.incremental = IncrementalPR(True)
        gitlab_provider.unreviewed_files_set = {"a.py": {"new_path": "a.py"}}

        assert gitlab_provider.get_files() == ["a.py"]
        gitlab_provider.mr.changes.assert_not_called()

    def test_get_files_falls_back_to_mr_changes_when_not_incremental(self, gitlab_provider):
        gitlab_provider.incremental = IncrementalPR(False)
        gitlab_provider.git_files = None
        gitlab_provider.mr.changes.return_value = {"changes": [{"new_path": "x.py"}]}

        assert gitlab_provider.get_files() == ["x.py"]

    def test_get_previous_review_returns_most_recent_match(self, gitlab_provider):
        from pr_agent.algo.utils import PRReviewHeader

        # GitLab returns notes in created_at-DESC order. The helper relies on that order
        # (no local sort) — the unrelated newest note must be skipped, the newer matching
        # note must win over the older matching note.
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(3, "unrelated", "2024-06-01T10:00:00Z"),
            self._make_note(2, f"{PRReviewHeader.REGULAR.value} 🔍\nnew", "2024-05-01T10:00:00Z"),
            self._make_note(1, f"{PRReviewHeader.REGULAR.value} 🔍\nold", "2024-04-01T10:00:00Z"),
        ]

        result = gitlab_provider.get_previous_review(full=True, incremental=True)

        assert result is not None
        assert result.id == 2

    def test_master_merge_files_are_excluded_from_incremental_scope(self, gitlab_provider, mock_project):
        # Reproduction of the MR !1115 bug: user ran `git merge master` on the feature branch,
        # which brought CI/config changes into the branch via a merge commit. Those files are
        # NOT part of mr.changes() (the MR's actual contribution against its merge-base), but
        # repository_compare(last_seen, head) walks through the merge and surfaces them.
        # The fix intersects with mr.changes() to drop these "phantom" files.
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(7, "## PR Reviewer Guide 🔍\nbody", "2024-05-01T10:00:00Z"),
        ]
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("merge", "2024-05-01T11:30:00Z"),  # merge from master
            self._make_commit("feat",  "2024-05-01T11:00:00Z"),  # author commit on feature
            self._make_commit("c0",    "2024-05-01T09:00:00Z"),  # anchor (pre-review)
        ]
        mock_project.repository_compare.return_value = {
            "diffs": [
                # MR's own change to a frontend file — must be reviewed
                {"new_path": "src/feature.js", "old_path": "src/feature.js", "diff": "@@ ... @@",
                 "new_file": False, "deleted_file": False, "renamed_file": False},
                # Pulled in via merge from master, NOT part of the MR — must be excluded
                {"new_path": ".gitlab-ci.yml", "old_path": ".gitlab-ci.yml", "diff": "@@ ... @@",
                 "new_file": False, "deleted_file": False, "renamed_file": False},
            ]
        }
        # mr.changes() returns only the MR's actual contribution; .gitlab-ci.yml is absent
        # because the change to it lives in the target branch already.
        gitlab_provider.mr.changes.return_value = {
            "changes": [{"new_path": "src/feature.js"}]
        }

        gitlab_provider.get_incremental_commits(IncrementalPR(True))

        assert gitlab_provider.incremental.is_incremental is True
        assert set(gitlab_provider.unreviewed_files_set.keys()) == {"src/feature.js"}
        assert ".gitlab-ci.yml" not in gitlab_provider.unreviewed_files_set

    def test_commit_with_unparseable_date_is_skipped_not_anchored(self, gitlab_provider, mock_project):
        # Anchor commit (c0) has a valid date older than the review; a stray dateless
        # commit (cX) sits between the new commits and must not become last_seen_commit.
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(7, "## PR Reviewer Guide 🔍\nbody", "2024-05-01T10:00:00Z"),
        ]
        bad_commit = MagicMock(spec=["id", "committed_date", "authored_date", "created_at"])
        bad_commit.id = "cX"
        bad_commit.committed_date = "not-a-date"
        bad_commit.authored_date = None
        bad_commit.created_at = None
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c1", "2024-05-01T11:00:00Z"),
            bad_commit,
            self._make_commit("c0", "2024-05-01T09:00:00Z"),
        ]
        mock_project.repository_compare.return_value = {
            "diffs": [{"new_path": "a.py", "old_path": "a.py", "diff": "@@ ... @@",
                       "new_file": False, "deleted_file": False, "renamed_file": False}],
        }

        gitlab_provider.get_incremental_commits(IncrementalPR(True))

        # The dateless commit must be ignored: anchor falls through to c0 (valid date).
        assert gitlab_provider.incremental.is_incremental is True
        assert gitlab_provider.incremental.last_seen_commit_sha == "c0"
        assert gitlab_provider.incremental.last_seen_commit.commit.author.date is not None
        assert gitlab_provider.incremental.first_new_commit_sha == "c1"

    def test_unparseable_review_timestamp_falls_back_to_full(self, gitlab_provider, mock_project):
        # If the previous review's created_at didn't parse, we can't position commits on the
        # timeline; we must fall back to a full review rather than silently report "no new files".
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(7, "## PR Reviewer Guide 🔍\nbody", "not-a-date"),
        ]
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c1", "2024-05-01T11:00:00Z"),
        ]

        gitlab_provider.get_incremental_commits(IncrementalPR(True))

        assert gitlab_provider.incremental.is_incremental is False
        mock_project.repository_compare.assert_not_called()

    def test_all_post_review_commits_dateless_falls_back_to_full(self, gitlab_provider, mock_project):
        # If every commit after the previous review has an unparseable timestamp, we can't
        # anchor a last_seen_commit. The fix must fall back to full review, not produce a
        # spurious "Incremental Review Skipped" message.
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(7, "## PR Reviewer Guide 🔍\nbody", "2024-05-01T10:00:00Z"),
        ]
        bad1 = MagicMock(spec=["id", "committed_date", "authored_date", "created_at"])
        bad1.id, bad1.committed_date, bad1.authored_date, bad1.created_at = "cX1", None, None, None
        bad2 = MagicMock(spec=["id", "committed_date", "authored_date", "created_at"])
        bad2.id, bad2.committed_date, bad2.authored_date, bad2.created_at = "cX2", "garbage", None, None
        gitlab_provider.mr.commits.return_value = [bad1, bad2]

        gitlab_provider.get_incremental_commits(IncrementalPR(True))

        assert gitlab_provider.incremental.is_incremental is False
        mock_project.repository_compare.assert_not_called()

    def test_get_previous_review_caches_empty_notes_list(self, gitlab_provider):
        # An MR with no notes must still cache the result; falsy-checks would re-fetch each call.
        gitlab_provider.mr.notes.list.return_value = []

        first = gitlab_provider.get_previous_review(full=True, incremental=True)
        second = gitlab_provider.get_previous_review(full=True, incremental=True)

        assert first is None and second is None
        assert gitlab_provider.mr.notes.list.call_count == 1

    def test_incremental_kind_suggestions_anchors_on_suggestion_note(self, gitlab_provider, mock_project):
        # When kind="suggestions", we anchor on the latest /improve output (either the
        # "## PR Code Suggestions ✨" summary or an inline "**Suggestion:**" note),
        # NOT on a /review note posted later in the same CI run.
        gitlab_provider.mr.notes.list.return_value = [
            # Most recent: a review-incremental note posted AFTER the last /improve run.
            self._make_note(9, "## Incremental PR Reviewer Guide 🔍\nbody", "2026-05-15T10:05:00Z"),
            # The actual /improve anchor we want to pick.
            self._make_note(8, "**Suggestion:** Используйте Number вместо parseInt...", "2026-05-15T10:00:00Z"),
            # An older /review note that should NOT win over the suggestion above.
            self._make_note(5, "## PR Reviewer Guide 🔍\nold", "2026-05-15T09:00:00Z"),
        ]
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c2", "2026-05-15T10:10:00Z"),  # after the suggestion note
            self._make_commit("c0", "2026-05-15T09:30:00Z"),  # before the suggestion note
        ]
        mock_project.repository_compare.return_value = {
            "diffs": [{"new_path": "a.py", "old_path": "a.py", "diff": "@@ ... @@",
                       "new_file": False, "deleted_file": False, "renamed_file": False}],
        }
        gitlab_provider.mr.changes.return_value = {"changes": [{"new_path": "a.py"}]}

        gitlab_provider.get_incremental_commits(IncrementalPR(True), kind="suggestions")

        assert gitlab_provider.incremental.is_incremental is True
        assert gitlab_provider.incremental.first_new_commit_sha == "c2"
        assert gitlab_provider.incremental.last_seen_commit_sha == "c0"
        mock_project.repository_compare.assert_called_once_with("c0", "head")

    def test_incremental_kind_suggestions_falls_back_when_no_prior_suggestion(self, gitlab_provider, mock_project):
        # A /review note exists, but no /improve has ever run. /improve -i must fall back to
        # a full pass, not anchor on the review note.
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(5, "## PR Reviewer Guide 🔍\nbody", "2026-05-15T09:00:00Z"),
        ]
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c0", "2026-05-15T08:30:00Z"),
        ]

        gitlab_provider.get_incremental_commits(IncrementalPR(True), kind="suggestions")

        assert gitlab_provider.incremental.is_incremental is False
        mock_project.repository_compare.assert_not_called()

    def test_get_incremental_commits_default_kind_is_review(self, gitlab_provider, mock_project):
        # Sanity-check backward compatibility: no kind kwarg ⇒ behaves like a review run.
        gitlab_provider.mr.notes.list.return_value = [
            # A /improve note that must be IGNORED in default (review) mode.
            self._make_note(8, "**Suggestion:** xyz", "2026-05-15T10:00:00Z"),
        ]
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c0", "2026-05-15T09:30:00Z"),
        ]

        gitlab_provider.get_incremental_commits(IncrementalPR(True))

        # No review note exists -> fallback to full review (NOT anchoring on the suggestion note).
        assert gitlab_provider.incremental.is_incremental is False
        mock_project.repository_compare.assert_not_called()

    def test_incremental_get_diff_files_expands_submodule_changes(self, gitlab_provider):
        # Set up incremental state directly to isolate get_diff_files behaviour.
        gitlab_provider.incremental = IncrementalPR(True)
        gitlab_provider.unreviewed_files_set = {
            "libs/sub": {"new_path": "libs/sub", "old_path": "libs/sub",
                          "diff": "-Subproject commit aaa\n+Subproject commit bbb\n",
                          "new_file": False, "deleted_file": False, "renamed_file": False}
        }
        gitlab_provider._incremental_head_sha = "head"
        gitlab_provider.incremental.last_seen_commit = _GitlabIncrementalCommit(
            self._make_commit("c0", "2024-05-01T09:00:00Z")
        )

        expanded = [{
            "new_path": "libs/sub/file.py", "old_path": "libs/sub/file.py",
            "diff": "@@ ... @@", "new_file": False, "deleted_file": False, "renamed_file": False,
        }]
        with patch.object(gitlab_provider, "_expand_submodule_changes", return_value=expanded) as m_exp, \
             patch.object(gitlab_provider, "get_pr_file_content", return_value=""):
            files = gitlab_provider.get_diff_files()

        # _expand_submodule_changes was called with the incremental raw_changes,
        # and the resulting file list reflects the expanded entries.
        m_exp.assert_called_once()
        assert [f.filename for f in files] == ["libs/sub/file.py"]
