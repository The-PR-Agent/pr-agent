from datetime import datetime
from unittest.mock import MagicMock, call, patch

import pytest
from gitlab import Gitlab
from gitlab.exceptions import GitlabGetError
from gitlab.v4.objects import ProjectFile, ProjectMergeRequest, ProjectMergeRequestManager

from pr_agent.algo.utils import (PRCodeSuggestionsIdentity, PRReviewHeader,
                                 PRReviewIdentity)
from pr_agent.git_providers.git_provider import IncrementalPR
from pr_agent.git_providers.gitlab_provider import (
    GitLabProvider,
    _GitLabIncrementalCommit,
    _GitLabIncrementalNote,
    _parse_gitlab_iso_datetime,
)


def _mock_settings(publish_review_as_thread=False, resolve_outdated_inline_threads=False):
    """Settings stub whose .get() returns the GitLab thread flags and passes other keys through to the default."""
    settings = MagicMock()
    settings.get.side_effect = lambda key, default=None: {
        "GITLAB.PUBLISH_REVIEW_AS_THREAD": publish_review_as_thread,
        "GITLAB.RESOLVE_OUTDATED_INLINE_THREADS": resolve_outdated_inline_threads,
    }.get(key, default)
    return settings


_BOT_USER_ID = 7
_CURRENT_HEAD_SHA = "head-current"
_OUTDATED_HEAD_SHA = "head-old"


_AGENT_BODY = "**Suggestion:** Rename this [best practice, importance: 5]\n```suggestion\nx = 1\n```"
_HUMAN_BODY = "Please rename this variable before we merge."


def _thread_note(author_id=_BOT_USER_ID, system=False, resolved=False, resolvable=True,
                 with_position=True, head_sha=_OUTDATED_HEAD_SHA, position_type='text',
                 line_key='new_line', body=_AGENT_BODY):
    note = {'author': {'id': author_id}, 'system': system, 'resolved': resolved,
            'resolvable': resolvable, 'body': body}
    if with_position:
        position = {'position_type': position_type, 'new_path': 'src/app.py',
                    'old_path': 'src/app.py'}
        if head_sha is not None:
            position['head_sha'] = head_sha
        if line_key:
            position[line_key] = 12
        note['position'] = position
    return note


def _thread(notes, discussion_id='d1'):
    discussion = MagicMock()
    discussion.id = discussion_id
    discussion.attributes = {'id': discussion_id, 'notes': notes}
    return discussion


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

    def test_get_repo_file_content_loads_from_mr_target_branch(self, gitlab_provider, mock_gitlab_client, mock_project):
        mock_project.default_branch = "main"
        gitlab_provider.mr = MagicMock(target_branch="release-1.0")
        mock_file = MagicMock(ProjectFile)
        mock_file.decode.return_value = b"repo context"
        mock_project.files.get.return_value = mock_file

        content = gitlab_provider.get_repo_file_content("AGENTS.md")

        assert content == "repo context"
        mock_gitlab_client.projects.get.assert_called_with("test/repo")
        mock_project.files.get.assert_called_once_with(file_path="AGENTS.md", ref="release-1.0")
        mock_file.decode.assert_called_once()

    def test_get_repo_file_content_from_default_branch_ignores_target(self, gitlab_provider, mock_project):
        mock_project.default_branch = "main"
        gitlab_provider.mr = MagicMock(target_branch="release-1.0")
        mock_file = MagicMock(ProjectFile)
        mock_file.decode.return_value = b"repo context"
        mock_project.files.get.return_value = mock_file

        content = gitlab_provider.get_repo_file_content("AGENTS.md", from_default_branch=True)

        assert content == "repo context"
        mock_project.files.get.assert_called_once_with(file_path="AGENTS.md", ref="main")

    def test_get_repo_file_content_falls_back_to_default_branch_without_mr(self, gitlab_provider, mock_project):
        mock_project.default_branch = "main"
        gitlab_provider.mr = None
        mock_file = MagicMock(ProjectFile)
        mock_file.decode.return_value = b"repo context"
        mock_project.files.get.return_value = mock_file

        content = gitlab_provider.get_repo_file_content("AGENTS.md")

        assert content == "repo context"
        mock_project.files.get.assert_called_once_with(file_path="AGENTS.md", ref="main")

    def test_get_repo_file_content_treats_missing_file_as_empty(self, gitlab_provider, mock_project):
        mock_project.default_branch = "main"
        gitlab_provider.mr = MagicMock(target_branch="main")
        mock_project.files.get.side_effect = GitlabGetError("404 Not Found", response_code=404)

        content = gitlab_provider.get_repo_file_content("AGENTS.md")

        assert content == ""

    def test_get_repo_file_content_propagates_non_404_errors(self, gitlab_provider, mock_project):
        mock_project.default_branch = "main"
        gitlab_provider.mr = MagicMock(target_branch="main")
        mock_project.files.get.side_effect = GitlabGetError(
            "500 Server Error", response_code=500, response_body=b"upstream failure"
        )

        with pytest.raises(GitlabGetError, match="500 Server Error"):
            gitlab_provider.get_repo_file_content("AGENTS.md")

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

    def test_publish_description_with_none_title_leaves_title_unchanged(self, gitlab_provider):
        gitlab_provider.mr = MagicMock()
        gitlab_provider.mr.title = "Original title"
        gitlab_provider.id_mr = 1

        gitlab_provider.publish_description(None, "Updated description")

        # Title must not be overwritten when pr_title is None; only the body updates.
        assert gitlab_provider.mr.title == "Original title"
        assert gitlab_provider.mr.description == "Updated description"
        gitlab_provider.mr.save.assert_called_once()

    def test_publish_description_with_title_updates_both(self, gitlab_provider):
        gitlab_provider.mr = MagicMock()
        gitlab_provider.mr.title = "Original title"
        gitlab_provider.id_mr = 1

        gitlab_provider.publish_description("AI title", "Updated description")

        assert gitlab_provider.mr.title == "AI title"
        assert gitlab_provider.mr.description == "Updated description"
        gitlab_provider.mr.save.assert_called_once()

    @pytest.mark.parametrize("configured", [True, False])
    def test_should_publish_review_as_thread_reflects_config(self, gitlab_provider, configured):
        with patch("pr_agent.git_providers.gitlab_provider.get_settings",
                   return_value=_mock_settings(publish_review_as_thread=configured)):
            assert gitlab_provider.should_publish_review_as_thread() is configured

    def test_should_publish_review_as_thread_defaults_false(self, gitlab_provider):
        # Key absent -> default False (the feature is opt-in).
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: default
        with patch("pr_agent.git_providers.gitlab_provider.get_settings", return_value=settings):
            assert gitlab_provider.should_publish_review_as_thread() is False

    def test_publish_comment_defaults_to_a_note(self, gitlab_provider):
        # Without as_thread (status comments, other tools), publishing stays a plain note.
        gitlab_provider.mr = MagicMock()
        result = gitlab_provider.publish_comment("a status comment")

        gitlab_provider.mr.notes.create.assert_called_once_with({'body': 'a status comment'})
        gitlab_provider.mr.discussions.create.assert_not_called()
        assert result is gitlab_provider.mr.notes.create.return_value

    def test_publish_comment_as_thread_creates_a_discussion(self, gitlab_provider):
        gitlab_provider.mr = MagicMock()
        gitlab_provider.mr.discussions.create.return_value.attributes = {'notes': [{'id': 42}]}
        result = gitlab_provider.publish_comment("the review", as_thread=True)

        # A resolvable thread (discussion) is opened instead of a plain note...
        gitlab_provider.mr.discussions.create.assert_called_once_with({'body': 'the review'})
        gitlab_provider.mr.notes.create.assert_not_called()
        # ...and the thread's underlying note is returned so callers keep note-level semantics.
        gitlab_provider.mr.notes.get.assert_called_once_with(42)
        assert result is gitlab_provider.mr.notes.get.return_value

    def test_publish_comment_as_thread_falls_back_to_note_on_error(self, gitlab_provider):
        gitlab_provider.mr = MagicMock()
        gitlab_provider.mr.discussions.create.side_effect = Exception("gitlab api error")
        result = gitlab_provider.publish_comment("the review", as_thread=True)

        # Thread creation failed, so publishing must not raise and must fall back to a plain note.
        gitlab_provider.mr.notes.create.assert_called_once_with({'body': 'the review'})
        assert result is gitlab_provider.mr.notes.create.return_value

    @pytest.mark.parametrize("break_response", [
        lambda mr: setattr(mr.notes.get, 'side_effect', Exception("gitlab api error")),
        lambda mr: setattr(mr.discussions.create.return_value, 'attributes', {'notes': []}),
        lambda mr: setattr(mr.discussions.create.return_value, 'attributes', {}),
    ])
    def test_publish_comment_as_thread_returns_none_when_note_fetch_fails(self, gitlab_provider, break_response):
        # The thread was created; a failure fetching its note (API error or unexpected response
        # shape) must return None - not raise, and not post the review a second time as a plain note.
        gitlab_provider.mr = MagicMock()
        gitlab_provider.mr.discussions.create.return_value.attributes = {'notes': [{'id': 42}]}
        break_response(gitlab_provider.mr)

        result = gitlab_provider.publish_comment("the review", as_thread=True)

        assert result is None
        gitlab_provider.mr.discussions.create.assert_called_once()
        gitlab_provider.mr.notes.create.assert_not_called()

    def test_publish_comment_as_thread_is_ignored_for_temporary(self, gitlab_provider):
        gitlab_provider.mr = MagicMock()
        with patch("pr_agent.git_providers.gitlab_provider.get_settings",
                   return_value=_mock_settings(publish_review_as_thread=True)):
            result = gitlab_provider.publish_comment("Preparing review...", is_temporary=True, as_thread=True)

        # Temporary progress comments are removed shortly after, so they are never threaded.
        gitlab_provider.mr.discussions.create.assert_not_called()
        gitlab_provider.mr.notes.create.assert_called_once_with({'body': 'Preparing review...'})
        assert result in gitlab_provider.temp_comments

    def test_publish_review_as_thread_opens_a_new_thread_each_call(self, gitlab_provider):
        # persistent_comment=false: the reviewer calls publish_comment(as_thread=True) on every run,
        # so each review opens a fresh thread rather than editing or reusing a previous one.
        gitlab_provider.mr = MagicMock()
        gitlab_provider.mr.discussions.create.return_value.attributes = {'notes': [{'id': 1}]}
        gitlab_provider.publish_comment("first review", as_thread=True)
        gitlab_provider.publish_comment("second review", as_thread=True)

        assert gitlab_provider.mr.discussions.create.call_count == 2
        gitlab_provider.mr.discussions.create.assert_any_call({'body': 'first review'})
        gitlab_provider.mr.discussions.create.assert_any_call({'body': 'second review'})
        gitlab_provider.mr.notes.update.assert_not_called()

    def test_persistent_review_opens_a_thread_on_first_run(self, gitlab_provider):
        # persistent_comment=true, no existing review yet: the fallback create must open a thread.
        gitlab_provider.mr = MagicMock()
        gitlab_provider.mr.discussions.create.return_value.attributes = {'notes': [{'id': 5}]}
        gitlab_provider.get_issue_comments = MagicMock(return_value=[])
        gitlab_provider.publish_persistent_comment("## PR Review\n\nbody",
                                                   initial_header="## PR Review",
                                                   update_header=True,
                                                   final_update_message=False,
                                                   as_thread=True)

        gitlab_provider.mr.discussions.create.assert_called_once()
        gitlab_provider.mr.notes.create.assert_not_called()

    def test_persistent_review_update_edits_in_place_and_reopens_thread(self, gitlab_provider):
        # persistent_comment=true with an existing review thread: edit it in place
        # and reopen (unresolve) it
        header = "## PR Review"
        existing = MagicMock()
        existing.body = f"{header}\n\nprevious review"
        gitlab_provider.mr = MagicMock()
        gitlab_provider.get_issue_comments = MagicMock(return_value=[existing])
        gitlab_provider.get_latest_commit_url = MagicMock(return_value="https://gitlab.com/c/abc")
        gitlab_provider.get_comment_url = MagicMock(return_value="https://gitlab.com/n/1")
        gitlab_provider.unresolve_comment_thread = MagicMock()
        gitlab_provider.publish_persistent_comment(f"{header}\n\nnew review",
                                                   initial_header=header,
                                                   update_header=True,
                                                   final_update_message=False,
                                                   as_thread=True)

        gitlab_provider.mr.notes.update.assert_called_once()
        gitlab_provider.mr.discussions.create.assert_not_called()
        gitlab_provider.unresolve_comment_thread.assert_called_once_with(existing)

    def test_persistent_review_update_status_message_stays_a_plain_note(self, gitlab_provider):
        # final_update_message=true posts an "updated to latest commit" follow-up. It is a status
        # comment, so it stays a plain note even when the review itself is threaded.
        header = "## PR Review"
        existing = MagicMock()
        existing.body = f"{header}\n\nprevious review"
        gitlab_provider.mr = MagicMock()
        gitlab_provider.get_issue_comments = MagicMock(return_value=[existing])
        gitlab_provider.get_latest_commit_url = MagicMock(return_value="https://gitlab.com/c/abc")
        gitlab_provider.get_comment_url = MagicMock(return_value="https://gitlab.com/n/1")
        gitlab_provider.unresolve_comment_thread = MagicMock()
        gitlab_provider.publish_persistent_comment(f"{header}\n\nnew review",
                                                   initial_header=header,
                                                   update_header=True,
                                                   final_update_message=True,
                                                   as_thread=True)

        gitlab_provider.mr.discussions.create.assert_not_called()
        gitlab_provider.mr.notes.create.assert_called_once()
        assert "updated to latest commit" in gitlab_provider.mr.notes.create.call_args.args[0]['body']

    def test_persistent_review_update_does_not_duplicate_when_unresolve_raises(self, gitlab_provider):
        # A reopen failure after the in-place edit must not reach the outer fallback, which would
        # publish the review a second time.
        header = "## PR Review"
        existing = MagicMock()
        existing.body = f"{header}\n\nprevious review"
        gitlab_provider.mr = MagicMock()
        gitlab_provider.get_issue_comments = MagicMock(return_value=[existing])
        gitlab_provider.get_latest_commit_url = MagicMock(return_value="https://gitlab.com/c/abc")
        gitlab_provider.get_comment_url = MagicMock(return_value="https://gitlab.com/n/1")
        gitlab_provider.unresolve_comment_thread = MagicMock(side_effect=Exception("reopen failed"))
        gitlab_provider.publish_persistent_comment(f"{header}\n\nnew review",
                                                   initial_header=header,
                                                   update_header=True,
                                                   final_update_message=False,
                                                   as_thread=True)

        gitlab_provider.mr.notes.update.assert_called_once()
        gitlab_provider.mr.discussions.create.assert_not_called()
        gitlab_provider.mr.notes.create.assert_not_called()

    def test_persistent_review_update_does_not_duplicate_when_status_message_fails(self, gitlab_provider):
        # The review was already updated successfully, so a failure publishing the optional status
        # message must not reach the outer fallback and publish the full review again.
        header = "## PR Review"
        existing = MagicMock()
        existing.body = f"{header}\n\nprevious review"
        gitlab_provider.mr = MagicMock()
        gitlab_provider.get_issue_comments = MagicMock(return_value=[existing])
        gitlab_provider.get_latest_commit_url = MagicMock(return_value="https://gitlab.com/c/abc")
        gitlab_provider.get_comment_url = MagicMock(return_value="https://gitlab.com/n/1")
        gitlab_provider.unresolve_comment_thread = MagicMock()
        gitlab_provider.mr.notes.create.side_effect = Exception("status publish failed")

        with patch("pr_agent.git_providers.git_provider.get_logger") as mock_get_logger:
            result = gitlab_provider.publish_persistent_comment_full(f"{header}\n\nnew review",
                                                                     initial_header=header,
                                                                     update_header=True,
                                                                     final_update_message=True,
                                                                     as_thread=True)

        assert result is existing
        gitlab_provider.mr.notes.update.assert_called_once()
        gitlab_provider.mr.notes.create.assert_called_once()
        assert "updated to latest commit" in gitlab_provider.mr.notes.create.call_args.args[0]["body"]
        gitlab_provider.mr.discussions.create.assert_not_called()
        mock_get_logger.return_value.opt.assert_called_once_with(exception=True)
        mock_get_logger.return_value.opt.return_value.warning.assert_called_once_with(
            "Failed to publish persistent review update message; review was already updated")

    def test_persistent_review_update_falls_back_when_edit_fails(self, gitlab_provider):
        # A failure before the existing review is updated must retain the full-review fallback.
        header = "## PR Review"
        existing = MagicMock()
        existing.body = f"{header}\n\nprevious review"
        gitlab_provider.mr = MagicMock()
        gitlab_provider.mr.discussions.create.return_value.attributes = {"notes": [{"id": 42}]}
        gitlab_provider.get_issue_comments = MagicMock(return_value=[existing])
        gitlab_provider.get_latest_commit_url = MagicMock(return_value="https://gitlab.com/c/abc")
        gitlab_provider.get_comment_url = MagicMock(return_value="https://gitlab.com/n/1")
        gitlab_provider.edit_comment = MagicMock(side_effect=Exception("edit failed"))

        gitlab_provider.publish_persistent_comment_full(f"{header}\n\nnew review",
                                                        initial_header=header,
                                                        update_header=True,
                                                        final_update_message=True,
                                                        as_thread=True)

        gitlab_provider.edit_comment.assert_called_once()
        gitlab_provider.mr.discussions.create.assert_called_once_with({"body": f"{header}\n\nnew review"})
        gitlab_provider.mr.notes.create.assert_not_called()

    def test_persistent_review_update_without_thread_keeps_resolution(self, gitlab_provider):
        # Without as_thread (the persistent comment isn't a thread), resolution state must not be touched.
        header = "## PR Review"
        existing = MagicMock()
        existing.body = f"{header}\n\nprevious review"
        gitlab_provider.mr = MagicMock()
        gitlab_provider.get_issue_comments = MagicMock(return_value=[existing])
        gitlab_provider.get_latest_commit_url = MagicMock(return_value="https://gitlab.com/c/abc")
        gitlab_provider.get_comment_url = MagicMock(return_value="https://gitlab.com/n/1")
        gitlab_provider.unresolve_comment_thread = MagicMock()
        gitlab_provider.publish_persistent_comment(f"{header}\n\nnew review",
                                                   initial_header=header,
                                                   update_header=True,
                                                   final_update_message=False)

        gitlab_provider.mr.notes.update.assert_called_once()
        gitlab_provider.unresolve_comment_thread.assert_not_called()

    def test_persistent_review_migrates_legacy_heading_to_stable_identity(self, gitlab_provider):
        legacy = MagicMock(id=7)
        legacy.body = "## PR Reviewer Guide 🔍\n\nprevious review"
        gitlab_provider.mr = MagicMock()
        gitlab_provider.get_issue_comments = MagicMock(return_value=[legacy])
        gitlab_provider.get_latest_commit_url = MagicMock(return_value="https://gitlab.com/c/abc")
        gitlab_provider.get_comment_url = MagicMock(return_value="https://gitlab.com/n/7")

        gitlab_provider.publish_persistent_comment(
            "## Guideline Compliance Check 🔍\n\nnew review",
            initial_header="## Guideline Compliance Check 🔍",
            update_header=True,
            final_update_message=False,
            identity_marker=PRReviewIdentity.REGULAR.value,
            legacy_initial_header=f"{PRReviewHeader.REGULAR.value} 🔍",
        )

        gitlab_provider.mr.notes.update.assert_called_once()
        updated_body = gitlab_provider.mr.notes.update.call_args.args[1]["body"]
        assert updated_body.startswith("## Guideline Compliance Check 🔍\n\n")
        assert PRReviewIdentity.REGULAR.value in updated_body
        gitlab_provider.mr.notes.create.assert_not_called()

    def test_persistent_review_prefers_marked_comment_over_legacy_comment(self, gitlab_provider):
        legacy = MagicMock(id=1)
        legacy.body = "## PR Reviewer Guide 🔍\n\nlegacy review"
        marked = MagicMock(id=2)
        marked.body = (
            "## Old Custom Heading 🔍\n\n"
            f"{PRReviewIdentity.REGULAR.value}\n\nmarked review"
        )
        gitlab_provider.mr = MagicMock()
        gitlab_provider.get_issue_comments = MagicMock(return_value=[legacy, marked])
        gitlab_provider.get_latest_commit_url = MagicMock(return_value="https://gitlab.com/c/abc")
        gitlab_provider.get_comment_url = MagicMock(return_value="https://gitlab.com/n/2")

        gitlab_provider.publish_persistent_comment(
            "## New Custom Heading 🔍\n\nnew review",
            initial_header="## New Custom Heading 🔍",
            update_header=True,
            final_update_message=False,
            identity_marker=PRReviewIdentity.REGULAR.value,
            legacy_initial_header=f"{PRReviewHeader.REGULAR.value} 🔍",
        )

        assert gitlab_provider.mr.notes.update.call_args.args[0] == 2
        gitlab_provider.mr.notes.create.assert_not_called()

    def test_persistent_review_does_not_adopt_similar_human_heading(self, gitlab_provider):
        human_comment = MagicMock(id=3)
        human_comment.body = "## PR Reviewer Guide for maintainers\n\nhuman notes"
        gitlab_provider.mr = MagicMock()
        gitlab_provider.get_issue_comments = MagicMock(return_value=[human_comment])

        gitlab_provider.publish_persistent_comment(
            "## New Custom Heading 🔍\n\nnew review",
            initial_header="## New Custom Heading 🔍",
            update_header=True,
            final_update_message=False,
            identity_marker=PRReviewIdentity.REGULAR.value,
            legacy_initial_header=f"{PRReviewHeader.REGULAR.value} 🔍",
        )

        gitlab_provider.mr.notes.update.assert_not_called()
        gitlab_provider.mr.notes.create.assert_called_once()

    @pytest.mark.parametrize("resolvable,resolved,should_reopen", [
        (True, True, True),     # resolved thread -> reopen it
        (True, False, False),   # already open -> leave it
        (False, False, False),  # not resolvable -> nothing to do
    ])
    def test_unresolve_comment_thread(self, gitlab_provider, resolvable, resolved, should_reopen):
        comment = MagicMock(id=42)
        discussion = MagicMock()
        discussion.attributes = {'notes': [{'id': 42, 'resolvable': resolvable, 'resolved': resolved}]}
        gitlab_provider.mr = MagicMock()
        gitlab_provider.mr.discussions.list.return_value = [discussion]

        gitlab_provider.unresolve_comment_thread(comment)

        if should_reopen:
            assert discussion.resolved is False
            discussion.save.assert_called_once()
        else:
            discussion.save.assert_not_called()

    @pytest.mark.parametrize("note_attrs", [
        {'resolved': False},      # note not resolved -> nothing to reopen
        {'resolvable': False},    # note not resolvable -> nothing to reopen
    ])
    def test_unresolve_comment_thread_skips_discussion_scan_when_note_not_resolved(self, gitlab_provider, note_attrs):
        # The note's own resolution state rules out a resolved thread, so the (paginated)
        # discussions listing must be skipped entirely.
        comment = MagicMock(id=42, **note_attrs)
        gitlab_provider.mr = MagicMock()

        gitlab_provider.unresolve_comment_thread(comment)

        gitlab_provider.mr.discussions.list.assert_not_called()

    def test_unresolve_comment_thread_ignores_unrelated_discussions(self, gitlab_provider):
        # A resolved discussion that does not own our note must be left untouched.
        comment = MagicMock(id=99)
        other = MagicMock()
        other.attributes = {'notes': [{'id': 1, 'resolvable': True, 'resolved': True}]}
        gitlab_provider.mr = MagicMock()
        gitlab_provider.mr.discussions.list.return_value = [other]

        gitlab_provider.unresolve_comment_thread(comment)

        other.save.assert_not_called()

    def test_unresolve_comment_thread_soft_fails(self, gitlab_provider):
        # A GitLab API error while reopening must not raise.
        gitlab_provider.mr = MagicMock()
        gitlab_provider.mr.discussions.list.side_effect = Exception("gitlab api error")

        gitlab_provider.unresolve_comment_thread(MagicMock(id=1))  # must not raise

    def _prepare_outdated_cleanup(self, gitlab_provider, threads, own_user_id=_BOT_USER_ID,
                                  current_head_sha=_CURRENT_HEAD_SHA):
        gitlab_provider._own_user_id = own_user_id
        gitlab_provider.mr = MagicMock()
        gitlab_provider.mr.diff_refs = {'head_sha': current_head_sha}
        gitlab_provider.mr.discussions.list.return_value = threads

    def _run_outdated_cleanup(self, gitlab_provider, enabled=True):
        with patch("pr_agent.git_providers.gitlab_provider.get_settings",
                   return_value=_mock_settings(resolve_outdated_inline_threads=enabled)):
            gitlab_provider.resolve_outdated_inline_threads()

    def test_resolve_outdated_inline_threads_is_opt_in(self, gitlab_provider):
        outdated = _thread([_thread_note()])
        self._prepare_outdated_cleanup(gitlab_provider, [outdated])

        self._run_outdated_cleanup(gitlab_provider, enabled=False)

        gitlab_provider.mr.discussions.list.assert_not_called()
        outdated.save.assert_not_called()

    def test_resolve_outdated_inline_threads_resolves_only_the_bots_outdated_threads(self, gitlab_provider):
        outdated = _thread([_thread_note()], discussion_id='outdated')
        current = _thread([_thread_note(head_sha=_CURRENT_HEAD_SHA)], discussion_id='current')
        with_human_reply = _thread([_thread_note(),
                                    _thread_note(author_id=99, with_position=False)],
                                   discussion_id='human')
        self._prepare_outdated_cleanup(gitlab_provider, [outdated, current, with_human_reply])

        self._run_outdated_cleanup(gitlab_provider)

        assert outdated.resolved is True
        outdated.save.assert_called_once()
        current.save.assert_not_called()
        with_human_reply.save.assert_not_called()

    def test_resolve_outdated_inline_threads_ignores_gitlab_system_notes(self, gitlab_provider):
        outdated = _thread([_thread_note(),
                            _thread_note(author_id=99, system=True, with_position=False)])
        self._prepare_outdated_cleanup(gitlab_provider, [outdated])

        self._run_outdated_cleanup(gitlab_provider)

        assert outdated.resolved is True
        outdated.save.assert_called_once()

    def test_resolve_outdated_inline_threads_accepts_a_marked_body_without_the_suggestion_lead(self, gitlab_provider):
        marked = _thread([_thread_note(body="**Possible Issue**\n\nx\n\n<!-- pr-agent-dedup: a1b2c3d4e5f6 -->")])
        self._prepare_outdated_cleanup(gitlab_provider, [marked])

        self._run_outdated_cleanup(gitlab_provider)

        assert marked.resolved is True
        marked.save.assert_called_once()

    @pytest.mark.parametrize("notes", [
        pytest.param([], id="no_notes"),
        pytest.param(["not-a-dict"], id="opener_not_a_dict"),
        pytest.param([_thread_note(resolved=True)], id="already_resolved"),
        pytest.param([_thread_note(resolvable=False)], id="not_resolvable"),
        pytest.param([_thread_note(with_position=False)], id="no_position"),
        pytest.param([_thread_note(position_type='image')], id="not_a_text_position"),
        pytest.param([_thread_note(line_key=None)], id="not_line_anchored"),
        pytest.param([_thread_note(head_sha=None)], id="no_recorded_head_sha"),
        pytest.param([_thread_note(head_sha=_CURRENT_HEAD_SHA)], id="on_the_current_diff"),
        pytest.param([_thread_note(body=_HUMAN_BODY)], id="hand_written_by_the_token_owner"),
        pytest.param([_thread_note(body=None)], id="no_body"),
        pytest.param([_thread_note(body="<!-- pr-agent-dedup: in prose")], id="malformed_marker"),
        pytest.param([{'author': 'not-a-dict', 'body': _AGENT_BODY,
                       'position': {'position_type': 'text', 'new_line': 1,
                                    'head_sha': _OUTDATED_HEAD_SHA}}],
                     id="author_not_a_dict"),
        pytest.param([_thread_note(), 'not-a-dict'], id="reply_not_a_dict"),
    ])
    def test_resolve_outdated_inline_threads_leaves_unconfirmed_threads_alone(self, gitlab_provider, notes):
        thread = _thread(notes)
        self._prepare_outdated_cleanup(gitlab_provider, [thread])

        self._run_outdated_cleanup(gitlab_provider)

        thread.save.assert_not_called()

    @pytest.mark.parametrize("own_user_id,current_head_sha", [
        (None, _CURRENT_HEAD_SHA),
        (_BOT_USER_ID, None),
    ])
    def test_resolve_outdated_inline_threads_skips_when_identity_or_head_is_unknown(
            self, gitlab_provider, own_user_id, current_head_sha):
        outdated = _thread([_thread_note()])
        self._prepare_outdated_cleanup(gitlab_provider, [outdated], own_user_id=own_user_id,
                                       current_head_sha=current_head_sha)

        self._run_outdated_cleanup(gitlab_provider)

        gitlab_provider.mr.discussions.list.assert_not_called()
        outdated.save.assert_not_called()

    def test_resolve_outdated_inline_threads_continues_past_a_failing_thread(self, gitlab_provider):
        failing = _thread([_thread_note()], discussion_id='failing')
        failing.save.side_effect = Exception("gitlab api error")
        outdated = _thread([_thread_note()], discussion_id='outdated')
        self._prepare_outdated_cleanup(gitlab_provider, [failing, outdated])

        self._run_outdated_cleanup(gitlab_provider)

        outdated.save.assert_called_once()

    def test_resolve_outdated_inline_threads_soft_fails(self, gitlab_provider):
        self._prepare_outdated_cleanup(gitlab_provider, [])
        gitlab_provider.mr.discussions.list.side_effect = Exception("gitlab api error")

        self._run_outdated_cleanup(gitlab_provider)

    def test_publish_code_suggestions_resolves_outdated_inline_threads_first(self, gitlab_provider):
        gitlab_provider.resolve_outdated_inline_threads = MagicMock()
        gitlab_provider.send_inline_comment = MagicMock()

        assert gitlab_provider.publish_code_suggestions([]) is True

        gitlab_provider.resolve_outdated_inline_threads.assert_called_once_with()
        gitlab_provider.send_inline_comment.assert_not_called()

    # ---- publish_labels / get_pr_labels tests ----

    def _real_mr(self, snapshot_labels, update_result=None, update_error=None):
        """Build a real python-gitlab merge request object with ``snapshot_labels``.

        A MagicMock cannot stand in here: python-gitlab keeps attributes assigned on a
        RESTObject in ``_updated_attrs`` instead of ``__dict__``, which is exactly the
        behavior publish_labels has to clean up after. ``manager.update`` is stubbed so
        ``save()`` never leaves the process; it records the payload put on the wire and
        returns ``update_result`` as the server response (or raises ``update_error``).
        """
        manager = ProjectMergeRequestManager(Gitlab("https://gitlab.example.com"), parent=None)
        manager.update = MagicMock(return_value=update_result, side_effect=update_error)
        return ProjectMergeRequest(
            manager,
            {"id": 1, "iid": 1, "project_id": 1, "labels": list(snapshot_labels)},
            created_from_list=False,
        )

    @staticmethod
    def _wire_payload(mr):
        return mr.manager.update.call_args[0][1]

    def test_publish_labels_noop_when_sets_equal(self, gitlab_provider):
        gitlab_provider.mr = self._real_mr(["bug", "review effort 3/5"])

        gitlab_provider.publish_labels(["bug", "review effort 3/5"])

        gitlab_provider.mr.manager.update.assert_not_called()

    def test_publish_labels_adds_only_missing(self, gitlab_provider):
        gitlab_provider.mr = self._real_mr(
            ["bug"], update_result={"iid": 1, "labels": ["bug", "review effort 3/5"]}
        )

        gitlab_provider.publish_labels(["bug", "review effort 3/5"])

        payload = self._wire_payload(gitlab_provider.mr)
        assert payload["add_labels"] == "review effort 3/5"
        assert "remove_labels" not in payload
        # Reading mr.labels queues the whole array for saving unless it is cleared;
        # shipping it next to the diff would restore the overwrite being fixed here.
        assert "labels" not in payload

    def test_publish_labels_removes_stale_managed_labels(self, gitlab_provider):
        gitlab_provider.mr = self._real_mr(
            ["review effort 5/5", "Possible security concern"],
            update_result={"iid": 1, "labels": ["review effort 2/5"]},
        )

        gitlab_provider.publish_labels(["review effort 2/5"])

        payload = self._wire_payload(gitlab_provider.mr)
        assert payload["add_labels"] == "review effort 2/5"
        # sorted() keeps the comma-separated payload deterministic.
        assert payload["remove_labels"] == "Possible security concern,review effort 5/5"

    def test_publish_labels_leaves_labels_outside_the_snapshot_alone(self, gitlab_provider):
        # The bug this fixes: assigning mr.labels PUT the whole array, so a label the
        # user added after this snapshot was taken (here "area/backend", present on the
        # server but absent from the snapshot) was wiped. A diff can only touch labels
        # it names, so an unseen label is never removed.
        gitlab_provider.mr = self._real_mr(
            ["review effort 3/5"],
            update_result={"iid": 1, "labels": ["area/backend", "review effort 4/5"]},
        )

        gitlab_provider.publish_labels(["review effort 4/5"])

        payload = self._wire_payload(gitlab_provider.mr)
        assert payload["remove_labels"] == "review effort 3/5"
        assert "labels" not in payload

    def test_publish_labels_refreshes_cached_labels_from_the_response(self, gitlab_provider):
        gitlab_provider.mr = self._real_mr(
            ["review effort 3/5"],
            update_result={"iid": 1, "labels": ["area/backend", "review effort 4/5"]},
        )

        gitlab_provider.publish_labels(["review effort 4/5"])

        assert gitlab_provider.get_pr_labels() == ["area/backend", "review effort 4/5"]

    def test_publish_labels_leaves_no_pending_writes_on_a_noop(self, gitlab_provider):
        # Nothing to publish, but the labels read still has to leave the MR clean:
        # publish_description() saves the same object right afterwards.
        gitlab_provider.mr = self._real_mr(["bug"])

        gitlab_provider.publish_labels(["bug"])

        assert gitlab_provider.mr._get_updated_data() == {}

    def test_reading_labels_leaves_no_pending_writes(self, gitlab_provider):
        gitlab_provider.mr = self._real_mr(["bug"])

        assert gitlab_provider.get_pr_labels() == ["bug"]
        assert gitlab_provider.mr._get_updated_data() == {}

    def test_labels_are_readable_from_an_mr_without_pending_attrs(self, gitlab_provider):
        # Clearing pending writes reaches into python-gitlab's internals, so a merge
        # request that does not carry them must not break the read.
        class _PlainMR:
            labels = ["bug"]

        gitlab_provider.mr = _PlainMR()

        assert gitlab_provider.get_pr_labels() == ["bug"]

    def test_publish_labels_drops_pending_diff_when_save_fails(self, gitlab_provider):
        # save() clears pending attributes itself, but only when it succeeds. If they
        # survive a failure, the next save() on this MR — publish_description() runs
        # one moments later — resends the label diff.
        gitlab_provider.mr = self._real_mr(["bug"], update_error=RuntimeError("network blip"))

        gitlab_provider.publish_labels(["review effort 3/5"])

        assert gitlab_provider.mr.manager.update.call_count == 1
        assert gitlab_provider.mr._get_updated_data() == {}

    def test_get_pr_labels_no_update_returns_cached(self, gitlab_provider):
        gitlab_provider.mr = MagicMock(labels=["cached"])
        gitlab_provider._get_merge_request = MagicMock()

        assert gitlab_provider.get_pr_labels(update=False) == ["cached"]
        gitlab_provider._get_merge_request.assert_not_called()

    def test_get_pr_labels_with_update_refreshes(self, gitlab_provider):
        fresh_mr = MagicMock(labels=["fresh-from-server"])
        gitlab_provider.mr = MagicMock(labels=["cached-stale"])
        gitlab_provider._get_merge_request = MagicMock(return_value=fresh_mr)

        assert gitlab_provider.get_pr_labels(update=True) == ["fresh-from-server"]
        assert gitlab_provider.mr is fresh_mr

    def test_get_pr_labels_with_update_falls_back_to_cache_on_failure(self, gitlab_provider):
        # Label reads are best-effort across providers. Returning the snapshot is safe
        # because publish_labels diffs against that same snapshot, so a failed refresh
        # narrows what the update touches instead of clobbering labels.
        gitlab_provider.mr = MagicMock(labels=["cached"])
        gitlab_provider._get_merge_request = MagicMock(side_effect=RuntimeError("boom"))

        assert gitlab_provider.get_pr_labels(update=True) == ["cached"]


@pytest.fixture(autouse=True)
def _clear_global_settings_cache():
    # The group global-settings cache is process-level; clear it between tests.
    from pr_agent.git_providers import git_provider as _gp
    _gp._GLOBAL_SETTINGS_CACHE.clear()
    yield
    _gp._GLOBAL_SETTINGS_CACHE.clear()


class TestGitLabGlobalSettings:
    def _provider(self, gitlab_url="https://gitlab.com"):
        provider = GitLabProvider.__new__(GitLabProvider)
        provider.gl = MagicMock()
        provider.id_project = "mygroup/myrepo"
        provider.gitlab_url = gitlab_url
        return provider

    def test_loads_group_pr_agent_settings(self):
        provider = self._provider()
        proj = MagicMock()
        proj.default_branch = "main"
        proj.files.get.return_value.decode.return_value = b"[pr_reviewer]\nnum_max_findings = 5\n"
        provider.gl.projects.get.return_value = proj
        with patch("pr_agent.git_providers.gitlab_provider.get_settings") as ms:
            ms.return_value.config.use_global_settings_file = True
            result = provider._get_global_repo_settings()
        assert result == b"[pr_reviewer]\nnum_max_findings = 5\n"
        provider.gl.projects.get.assert_called_with("mygroup/pr-agent-settings")
        proj.files.get.assert_called_once_with(file_path=".pr_agent.toml", ref="main")

    def test_numeric_project_id_uses_canonical_group_for_global_settings(self):
        provider = self._provider()
        provider.id_project = "127014"
        project = MagicMock(path_with_namespace="mygroup/myrepo")
        settings_project = MagicMock()
        settings_project.default_branch = "main"
        settings_project.files.get.return_value.decode.return_value = b"[pr_reviewer]\nnum_max_findings = 5\n"
        provider.gl.projects.get.side_effect = [project, settings_project]

        with patch("pr_agent.git_providers.gitlab_provider.get_settings") as ms:
            ms.return_value.config.use_global_settings_file = True
            result = provider._get_global_repo_settings()

        assert result == b"[pr_reviewer]\nnum_max_findings = 5\n"
        assert provider.gl.projects.get.call_args_list[0].args == ("127014",)
        assert provider.gl.projects.get.call_args_list[1].args == ("mygroup/pr-agent-settings",)

    def test_skips_on_self_hosted(self):
        # "mygitlab.com" contains the substring "gitlab.com" but is NOT GitLab.com — must be skipped.
        provider = self._provider(gitlab_url="https://mygitlab.com")
        with patch("pr_agent.git_providers.gitlab_provider.get_settings") as ms:
            ms.return_value.config.use_global_settings_file = True
            assert provider._get_global_repo_settings() == ""
        provider.gl.projects.get.assert_not_called()

    def test_disabled_returns_empty(self):
        provider = self._provider()
        with patch("pr_agent.git_providers.gitlab_provider.get_settings") as ms:
            ms.return_value.config.use_global_settings_file = False
            assert provider._get_global_repo_settings() == ""
        provider.gl.projects.get.assert_not_called()

    def test_result_is_cached(self):
        provider = self._provider()
        proj = MagicMock()
        proj.default_branch = "main"
        proj.files.get.return_value.decode.return_value = b"[pr_reviewer]\nx = 1\n"
        provider.gl.projects.get.return_value = proj
        with patch("pr_agent.git_providers.gitlab_provider.get_settings") as ms:
            ms.return_value.config.use_global_settings_file = True
            provider._get_global_repo_settings()
            provider._get_global_repo_settings()
        # Only one lookup for the settings project despite two calls (cached).
        assert provider.gl.projects.get.call_count == 1

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

        adapter = _GitLabIncrementalCommit(gl_commit)

        assert adapter.sha == "abc123"
        # committed_date takes precedence over authored_date
        assert adapter.commit.author.date == datetime(2024, 5, 1, 10, 0, 0)

    def test_commit_adapter_falls_back_to_authored_date(self):
        gl_commit = MagicMock(spec=["id", "authored_date"])
        gl_commit.id = "abc"
        gl_commit.authored_date = "2024-04-30T10:00:00Z"

        adapter = _GitLabIncrementalCommit(gl_commit)

        assert adapter.commit.author.date == datetime(2024, 4, 30, 10, 0, 0)

    def test_note_adapter_builds_html_url(self):
        note = MagicMock()
        note.id = 42
        note.body = "## PR Reviewer Guide 🔍\n..."
        note.created_at = "2024-05-01T10:00:00Z"

        adapter = _GitLabIncrementalNote(note, mr_web_url="https://gitlab.com/x/y/-/merge_requests/1")

        assert adapter.id == 42
        assert adapter.html_url == "https://gitlab.com/x/y/-/merge_requests/1#note_42"
        assert adapter.created_at == datetime(2024, 5, 1, 10, 0, 0)

    def test_note_adapter_anchor_time_prefers_later_updated_at(self):
        # Persistent comments are edited in place: created_at stays frozen at the first
        # run while updated_at tracks the latest one. anchor_time must follow updated_at.
        note = MagicMock()
        note.id = 42
        note.body = "## PR Code Suggestions ✨\n..."
        note.created_at = "2024-05-01T10:00:00Z"
        note.updated_at = "2024-05-03T12:00:00Z"

        adapter = _GitLabIncrementalNote(note)

        assert adapter.created_at == datetime(2024, 5, 1, 10, 0, 0)
        assert adapter.updated_at == datetime(2024, 5, 3, 12, 0, 0)
        assert adapter.anchor_time == datetime(2024, 5, 3, 12, 0, 0)

    def test_note_adapter_anchor_time_falls_back_to_created_at(self):
        # No parseable updated_at -> anchor on created_at; nothing parseable -> None.
        note = MagicMock()
        note.id = 42
        note.body = "## PR Reviewer Guide 🔍\n..."
        note.created_at = "2024-05-01T10:00:00Z"
        note.updated_at = None

        adapter = _GitLabIncrementalNote(note)
        assert adapter.anchor_time == datetime(2024, 5, 1, 10, 0, 0)

        note.created_at = None
        assert _GitLabIncrementalNote(note).anchor_time is None


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
    def _make_note(note_id, body, created_at, updated_at=None, author=None):
        n = MagicMock()
        n.id = note_id
        n.body = body
        n.created_at = created_at
        n.updated_at = updated_at
        n.author = author
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
        assert set(gitlab_provider.unreviewed_files_map.keys()) == {"a.py", "b.py"}
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
        # unreviewed_files_map is empty.
        assert gitlab_provider.incremental.is_incremental is True
        assert gitlab_provider.unreviewed_files_map == {}
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
        gitlab_provider.unreviewed_files_map = {"a.py": {"new_path": "a.py"}}

        assert gitlab_provider.get_files() == ["a.py"]
        gitlab_provider.mr.changes.assert_not_called()

    def test_get_files_falls_back_to_mr_changes_when_not_incremental(self, gitlab_provider):
        gitlab_provider.incremental = IncrementalPR(False)
        gitlab_provider.git_files = None
        gitlab_provider.mr.changes.return_value = {"changes": [{"new_path": "x.py"}]}

        assert gitlab_provider.get_files() == ["x.py"]

    def test_get_files_retries_raw_diffs_when_changes_overflow(self, gitlab_provider):
        gitlab_provider.git_files = None
        gitlab_provider.mr.changes.side_effect = [
            {"changes": [{"new_path": "visible.py"}], "overflow": True},
            {
                "changes": [
                    {"new_path": "visible.py"},
                    {"new_path": "hidden.py"},
                ],
                "overflow": False,
            },
        ]

        assert gitlab_provider.get_files() == ["visible.py", "hidden.py"]
        assert gitlab_provider.mr.changes.call_args_list == [
            call(),
            call(access_raw_diffs=True),
        ]

    def test_get_diff_files_retries_raw_diffs_when_changes_overflow(self, gitlab_provider):
        change = {
            "old_path": "visible.py",
            "new_path": "visible.py",
            "diff": "@@ -1 +1 @@\n-old\n+new\n",
            "new_file": False,
            "deleted_file": False,
            "renamed_file": False,
        }
        hidden_change = {**change, "old_path": "hidden.py", "new_path": "hidden.py"}
        gitlab_provider.mr.changes.side_effect = [
            {"changes": [change], "overflow": True},
            {"changes": [change, hidden_change], "overflow": False},
        ]
        gitlab_provider.get_pr_file_content = MagicMock(return_value="")

        diff_files = gitlab_provider.get_diff_files()

        assert [file.filename for file in diff_files] == ["visible.py", "hidden.py"]
        assert gitlab_provider.mr.changes.call_args_list == [
            call(),
            call(access_raw_diffs=True),
        ]

    def test_incremental_scope_retries_raw_diffs_when_changes_overflow(
        self, gitlab_provider, mock_project
    ):
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(7, "## PR Reviewer Guide 🔍\nbody", "2024-05-01T10:00:00Z"),
        ]
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c1", "2024-05-01T11:00:00Z"),
            self._make_commit("c0", "2024-05-01T09:00:00Z"),
        ]
        mock_project.repository_compare.return_value = {
            "diffs": [
                {"new_path": "visible.py"},
                {"new_path": "hidden.py"},
            ]
        }
        gitlab_provider.mr.changes.side_effect = [
            {"changes": [{"new_path": "visible.py"}], "overflow": True},
            {
                "changes": [
                    {"new_path": "visible.py"},
                    {"new_path": "hidden.py"},
                ],
                "overflow": False,
            },
        ]

        gitlab_provider.get_incremental_commits(IncrementalPR(True))

        assert set(gitlab_provider.unreviewed_files_map) == {"visible.py", "hidden.py"}
        assert gitlab_provider.mr.changes.call_args_list == [
            call(),
            call(access_raw_diffs=True),
        ]

    def test_get_previous_review_returns_most_recent_match(self, gitlab_provider):
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

    def test_get_previous_review_accepts_custom_heading_with_stable_identity(self, gitlab_provider):
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(
                7,
                (
                    "## Guideline Compliance Check 🔍\n\n"
                    f"{PRReviewIdentity.REGULAR.value}\n\nbody"
                ),
                "2024-05-01T10:00:00Z",
            ),
        ]

        result = gitlab_provider.get_previous_review(full=True, incremental=False)

        assert result is not None
        assert result.id == 7

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
        assert set(gitlab_provider.unreviewed_files_map.keys()) == {"src/feature.js"}
        assert ".gitlab-ci.yml" not in gitlab_provider.unreviewed_files_map

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

    def test_object_shaped_compare_diffs_are_normalized_to_dicts(self, gitlab_provider, mock_project):
        # repository_compare may yield object-shaped entries; downstream consumers
        # (filter_ignored, get_diff_files) subscript entries as dicts, so the collector
        # must normalize objects instead of storing them as-is.
        from types import SimpleNamespace
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(7, "## PR Reviewer Guide 🔍\nbody", "2024-05-01T10:00:00Z"),
        ]
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c1", "2024-05-01T11:00:00Z"),
            self._make_commit("c0", "2024-05-01T09:00:00Z"),
        ]
        obj_diff = SimpleNamespace(new_path="a.py", old_path="a.py", diff="@@ ... @@",
                                   new_file=False, deleted_file=False, renamed_file=False)
        mock_project.repository_compare.return_value = SimpleNamespace(diffs=[obj_diff])
        gitlab_provider.mr.changes.return_value = {"changes": [{"new_path": "a.py"}]}

        gitlab_provider.get_incremental_commits(IncrementalPR(True))

        stored = gitlab_provider.unreviewed_files_map["a.py"]
        assert isinstance(stored, dict)
        assert stored["new_path"] == "a.py"
        assert stored["diff"] == "@@ ... @@"
        assert stored["deleted_file"] is False

    def test_anchor_note_from_another_user_is_skipped(self, gitlab_provider, mock_project):
        # A human comment that merely starts with "**Suggestion:**" must not shift the
        # anchor when the bot's own user id is known.
        gitlab_provider.gl.user.id = 42
        gitlab_provider.mr.notes.list.return_value = [
            # Newest: user-authored note that looks like a suggestion anchor.
            self._make_note(9, "**Suggestion:** try this instead", "2026-05-15T12:00:00Z",
                            author={"id": 999}),
            # The real bot-authored anchor, older.
            self._make_note(8, "## PR Code Suggestions ✨\ntable", "2026-05-15T10:00:00Z",
                            author={"id": 42}),
        ]
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c2", "2026-05-15T11:00:00Z"),  # between bot anchor and user note
            self._make_commit("c0", "2026-05-15T09:30:00Z"),
        ]
        mock_project.repository_compare.return_value = {
            "diffs": [{"new_path": "a.py", "old_path": "a.py", "diff": "@@ ... @@",
                       "new_file": False, "deleted_file": False, "renamed_file": False}],
        }
        gitlab_provider.mr.changes.return_value = {"changes": [{"new_path": "a.py"}]}

        gitlab_provider.get_incremental_commits(IncrementalPR(True), kind="suggestions")

        # Anchored on the bot note (10:00), so c2 (11:00) is in scope — the forged
        # 12:00 note would have wrongly excluded it.
        assert gitlab_provider.incremental.is_incremental is True
        assert gitlab_provider.incremental.first_new_commit_sha == "c2"

    def test_anchor_author_check_fails_open_when_user_unresolvable(self, gitlab_provider, mock_project):
        # Job-token auth can't resolve the current user; anchoring must stay prefix-only
        # rather than breaking incremental runs.
        gitlab_provider.gl.auth.side_effect = Exception("401 insufficient scope")
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(8, "## PR Code Suggestions ✨\ntable", "2026-05-15T10:00:00Z",
                            author={"id": 999}),
        ]
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c2", "2026-05-15T11:00:00Z"),
            self._make_commit("c0", "2026-05-15T09:30:00Z"),
        ]
        mock_project.repository_compare.return_value = {
            "diffs": [{"new_path": "a.py", "old_path": "a.py", "diff": "@@ ... @@",
                       "new_file": False, "deleted_file": False, "renamed_file": False}],
        }
        gitlab_provider.mr.changes.return_value = {"changes": [{"new_path": "a.py"}]}

        gitlab_provider.get_incremental_commits(IncrementalPR(True), kind="suggestions")

        assert gitlab_provider.incremental.is_incremental is True
        assert gitlab_provider.incremental.first_new_commit_sha == "c2"

    def test_empty_incremental_scope_exposes_unreviewed_files_map(self, gitlab_provider, mock_project):
        # PRReviewer's skip path checks `hasattr(provider, "unreviewed_files_map")` and its
        # falsiness (pr_reviewer.py), matching GithubProvider/AzureDevopsProvider. The name
        # must match exactly, or the "no new commits" path silently degrades to a full
        # review instead of publishing "Incremental Review Skipped".
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(7, "## PR Reviewer Guide 🔍\nbody", "2024-05-01T10:00:00Z"),
        ]
        # The only commit predates the review -> legitimately empty incremental scope.
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c0", "2024-05-01T09:00:00Z"),
        ]

        gitlab_provider.get_incremental_commits(IncrementalPR(True))

        assert gitlab_provider.incremental.is_incremental is True
        assert gitlab_provider.unreviewed_files_map == {}
        mock_project.repository_compare.assert_not_called()

    def test_supports_incremental_kind(self, gitlab_provider):
        assert gitlab_provider.supports_incremental_kind("review") is True
        assert gitlab_provider.supports_incremental_kind("suggestions") is True
        assert gitlab_provider.supports_incremental_kind("something-else") is False

    def test_get_incremental_commits_invalidates_cached_diff_files(self, gitlab_provider):
        # Server mode caches the provider per PR URL: a diff computed by an earlier command
        # (e.g. a full /review) must not leak into a later run with a different scope.
        gitlab_provider.diff_files = ["stale-diff"]

        gitlab_provider.get_incremental_commits(IncrementalPR(False))

        assert gitlab_provider.diff_files is None

    def test_incremental_suggestions_anchor_advances_with_in_place_edits(self, gitlab_provider, mock_project):
        # Default /improve config (persistent_comment=true, commitable_code_suggestions=false)
        # EDITS the "## PR Code Suggestions ✨" summary note in place on every run, so its
        # created_at stays frozen at the first run. The incremental window must anchor on
        # updated_at (the latest run), otherwise it grows from the first run and keeps
        # re-including commits that were already covered.
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(8, "## PR Code Suggestions ✨\ntable",
                            created_at="2026-05-15T09:00:00Z",   # first /improve run
                            updated_at="2026-05-15T11:00:00Z"),  # latest /improve run
        ]
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c2", "2026-05-15T11:30:00Z"),  # new since the latest run
            self._make_commit("c1", "2026-05-15T10:00:00Z"),  # already covered by the 11:00 run
            self._make_commit("c0", "2026-05-15T08:30:00Z"),  # predates the first run
        ]
        mock_project.repository_compare.return_value = {
            "diffs": [{"new_path": "a.py", "old_path": "a.py", "diff": "@@ ... @@",
                       "new_file": False, "deleted_file": False, "renamed_file": False}],
        }
        gitlab_provider.mr.changes.return_value = {"changes": [{"new_path": "a.py"}]}

        gitlab_provider.get_incremental_commits(IncrementalPR(True), kind="suggestions")

        # Only c2 is in scope; c1 must NOT be re-included (that was the reported bug).
        assert gitlab_provider.incremental.is_incremental is True
        assert gitlab_provider.incremental.first_new_commit_sha == "c2"
        assert gitlab_provider.incremental.last_seen_commit_sha == "c1"
        mock_project.repository_compare.assert_called_once_with("c1", "head")

    def test_incremental_suggestions_uses_newest_stable_or_legacy_anchor(
            self, gitlab_provider, mock_project):
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(
                9,
                "## PR Code Suggestions ✨\n\n<!-- aaa1111 -->\n\n<table>legacy</table>",
                "2026-05-15T12:00:00Z",
            ),
            self._make_note(
                8,
                "## Team Suggestions ✨\n\n"
                f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
                "<!-- bbb2222 -->\n\n<table>marked</table>",
                "2026-05-15T10:00:00Z",
            ),
        ]
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c2", "2026-05-15T13:00:00Z"),
            self._make_commit("c1", "2026-05-15T11:00:00Z"),
            self._make_commit("c0", "2026-05-15T09:00:00Z"),
        ]
        mock_project.repository_compare.return_value = {
            "diffs": [{"new_path": "a.py", "old_path": "a.py", "diff": "@@ ... @@",
                       "new_file": False, "deleted_file": False, "renamed_file": False}],
        }
        gitlab_provider.mr.changes.return_value = {"changes": [{"new_path": "a.py"}]}

        gitlab_provider.get_incremental_commits(IncrementalPR(True), kind="suggestions")

        assert gitlab_provider.incremental.is_incremental is True
        assert gitlab_provider.incremental.first_new_commit_sha == "c2"
        assert gitlab_provider.incremental.last_seen_commit_sha == "c1"
        mock_project.repository_compare.assert_called_once_with("c1", "head")

    def test_incremental_suggestions_uses_latest_activity_across_stable_and_legacy_anchors(
            self, gitlab_provider, mock_project):
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(
                9,
                "## PR Code Suggestions ✨\n\n<!-- aaa1111 -->\n\n<table>legacy</table>",
                "2026-05-15T12:00:00Z",
            ),
            self._make_note(
                8,
                "## Team Suggestions ✨\n\n"
                f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
                "<!-- bbb2222 -->\n\n<table>marked</table>",
                created_at="2026-05-15T09:00:00Z",
                updated_at="2026-05-15T15:00:00Z",
            ),
        ]
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c3", "2026-05-15T16:00:00Z"),
            self._make_commit("c2", "2026-05-15T14:00:00Z"),
            self._make_commit("c1", "2026-05-15T11:00:00Z"),
        ]
        mock_project.repository_compare.return_value = {
            "diffs": [{"new_path": "a.py", "old_path": "a.py", "diff": "@@ ... @@",
                       "new_file": False, "deleted_file": False, "renamed_file": False}],
        }
        gitlab_provider.mr.changes.return_value = {"changes": [{"new_path": "a.py"}]}

        gitlab_provider.get_incremental_commits(IncrementalPR(True), kind="suggestions")

        assert gitlab_provider.incremental.is_incremental is True
        assert gitlab_provider.incremental.first_new_commit_sha == "c3"
        assert gitlab_provider.incremental.last_seen_commit_sha == "c2"
        mock_project.repository_compare.assert_called_once_with("c2", "head")

    def test_incremental_suggestions_unparseable_newest_anchor_falls_back_to_full(
            self, gitlab_provider, mock_project):
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(
                9,
                "## PR Code Suggestions ✨\n\n<!-- aaa1111 -->\n\n<table>legacy</table>",
                "not-a-date",
            ),
            self._make_note(
                8,
                "## Team Suggestions ✨\n\n"
                f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
                "<!-- bbb2222 -->\n\n<table>marked</table>",
                "2026-05-15T10:00:00Z",
            ),
        ]
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c1", "2026-05-15T11:00:00Z"),
        ]

        gitlab_provider.get_incremental_commits(IncrementalPR(True), kind="suggestions")

        assert gitlab_provider.incremental.is_incremental is False
        mock_project.repository_compare.assert_not_called()

    def test_incremental_suggestions_uses_newer_custom_no_suggestions_result(
            self, gitlab_provider, mock_project):
        gitlab_provider.mr.notes.list.return_value = [
            self._make_note(
                9,
                "## Team Suggestions ✨\n\n"
                f"{PRCodeSuggestionsIdentity.NO_SUGGESTIONS.value}\n\n"
                "No code suggestions found for the PR.",
                "2026-05-15T12:00:00Z",
            ),
            self._make_note(
                8,
                "## Previous Suggestions ✨\n\n"
                f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
                "<!-- bbb2222 -->\n\n<table>marked</table>",
                "2026-05-15T10:00:00Z",
            ),
        ]
        gitlab_provider.mr.commits.return_value = [
            self._make_commit("c2", "2026-05-15T13:00:00Z"),
            self._make_commit("c1", "2026-05-15T11:00:00Z"),
        ]
        mock_project.repository_compare.return_value = {
            "diffs": [{"new_path": "a.py", "old_path": "a.py", "diff": "@@ ... @@",
                       "new_file": False, "deleted_file": False, "renamed_file": False}],
        }
        gitlab_provider.mr.changes.return_value = {"changes": [{"new_path": "a.py"}]}

        gitlab_provider.get_incremental_commits(IncrementalPR(True), kind="suggestions")

        assert gitlab_provider.incremental.is_incremental is True
        assert gitlab_provider.incremental.first_new_commit_sha == "c2"
        assert gitlab_provider.incremental.last_seen_commit_sha == "c1"
        mock_project.repository_compare.assert_called_once_with("c1", "head")

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
        gitlab_provider.unreviewed_files_map = {
            "libs/sub": {"new_path": "libs/sub", "old_path": "libs/sub",
                          "diff": "-Subproject commit aaa\n+Subproject commit bbb\n",
                          "new_file": False, "deleted_file": False, "renamed_file": False}
        }
        gitlab_provider._incremental_head_sha = "head"
        gitlab_provider.incremental.last_seen_commit = _GitLabIncrementalCommit(
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


class TestGitLabCapabilities:
    def _provider(self):
        provider = GitLabProvider.__new__(GitLabProvider)
        provider.mr = MagicMock()
        return provider

    def test_get_issue_comments_is_reported_as_supported(self):
        """The capability flag used to contradict the working implementation below it,
        which was the only thing blocking ``/answer`` on GitLab."""
        assert self._provider().is_supported("get_issue_comments") is True

    def test_get_issue_comments_returns_notes_oldest_first(self):
        provider = self._provider()
        # GitLab's notes API returns newest-first; the provider flips it to match GitHub.
        provider.mr.notes.list.return_value = ["newest", "middle", "oldest"]

        assert provider.get_issue_comments() == ["oldest", "middle", "newest"]

    @pytest.mark.parametrize("capability", [
        "create_inline_comment",
        "publish_inline_comments",
        "publish_file_comments",
    ])
    def test_unimplemented_capabilities_stay_unsupported(self, capability):
        assert self._provider().is_supported(capability) is False

    def test_gfm_markdown_is_supported(self):
        assert self._provider().is_supported("gfm_markdown") is True


class TestGitLabRelevantDiff:
    """Inline comment positions are pinned to a diff version's SHAs, so picking the wrong
    version anchors the comment to a superseded diff — the state a force-push leaves behind."""

    # the versions endpoint is ordered newest-first
    VERSIONS = ["newest", "older", "oldest"]

    def _provider(self, changes):
        provider = GitLabProvider.__new__(GitLabProvider)
        provider.mr = MagicMock()
        provider.mr.diffs.list.return_value = list(self.VERSIONS)
        provider.mr.changes.return_value = {"changes": changes}
        provider.last_diff = "latest-at-construction"
        return provider

    def test_matching_change_resolves_to_the_newest_version(self):
        provider = self._provider([{"new_path": "app.py", "diff": "@@\n+added line\n"}])

        assert provider.get_relevant_diff("app.py", "+added line") == "newest"

    def test_fallback_uses_the_latest_version_not_the_oldest(self):
        provider = self._provider([{"new_path": "other.py", "diff": "@@\n+unrelated\n"}])

        assert provider.get_relevant_diff("app.py", "+added line") == "latest-at-construction"

    def test_returns_none_when_the_mr_has_no_diff_versions(self):
        provider = self._provider([{"new_path": "app.py", "diff": "@@\n+added line\n"}])
        provider.mr.diffs.list.return_value = []

        assert provider.get_relevant_diff("app.py", "+added line") is None

    def test_set_merge_request_snapshots_the_newest_version(self):
        provider = GitLabProvider.__new__(GitLabProvider)
        mr = MagicMock()
        mr.diffs.list.return_value = list(self.VERSIONS)
        with patch.object(GitLabProvider, "_parse_merge_request_url", return_value=("group/repo", 1)), \
             patch.object(GitLabProvider, "_get_merge_request", return_value=mr):
            provider._set_merge_request("https://gitlab.com/group/repo/-/merge_requests/1")

        assert provider.last_diff == "newest"

    def test_fallback_is_logged_once_not_once_per_version(self):
        provider = self._provider([{"new_path": "other.py", "diff": "@@\n+unrelated\n"}])

        with patch("pr_agent.git_providers.gitlab_provider.get_logger") as mock_logger:
            provider.get_relevant_diff("app.py", "+added line")

        assert mock_logger.return_value.debug.call_count == 1


class TestGitLabProviderUrlParsing:
    """Cover MR URL parsing on GitLab instances deployed under a relative URL (sub-path).

    `__init__` performs network calls, so build the provider with `__new__`
    and exercise the pure helper only.
    """

    @staticmethod
    def _provider(gitlab_url="https://gitlab.com"):
        provider = GitLabProvider.__new__(GitLabProvider)
        provider.gitlab_url = gitlab_url
        return provider

    def test_parse_merge_request_url_plain(self):
        provider = self._provider("https://gitlab.com")
        assert provider._parse_merge_request_url(
            "https://gitlab.com/group/subgroup/repo/-/merge_requests/123") == ("group/subgroup/repo", 123)

    def test_parse_merge_request_url_strips_sub_path_prefix(self):
        provider = self._provider("https://host.local/gitlab")
        assert provider._parse_merge_request_url(
            "https://host.local/gitlab/shai/pr-agent/-/merge_requests/12") == ("shai/pr-agent", 12)

    def test_parse_merge_request_url_sub_path_nested_groups(self):
        provider = self._provider("https://host.local/gitlab")
        assert provider._parse_merge_request_url(
            "https://host.local/gitlab/group/sub/repo/-/merge_requests/9") == ("group/sub/repo", 9)

    def test_parse_merge_request_url_multi_segment_sub_path(self):
        provider = self._provider("https://gitlab.example.com/gitlab/app")
        assert provider._parse_merge_request_url(
            "https://gitlab.example.com/gitlab/app/shai/pr-agent/-/merge_requests/5") == ("shai/pr-agent", 5)

    def test_parse_merge_request_url_config_with_trailing_slash(self):
        provider = self._provider("https://host.local/gitlab/")
        assert provider._parse_merge_request_url(
            "https://host.local/gitlab/shai/pr-agent/-/merge_requests/12") == ("shai/pr-agent", 12)

    def test_parse_merge_request_url_matches_host_with_port(self):
        provider = self._provider("https://host.local:8443/gitlab")
        assert provider._parse_merge_request_url(
            "https://host.local:8443/gitlab/shai/pr-agent/-/merge_requests/12") == ("shai/pr-agent", 12)

    def test_parse_merge_request_url_prefix_on_other_host_is_not_stripped(self):
        provider = self._provider("https://host.local/gitlab")
        assert provider._parse_merge_request_url(
            "https://other.example/gitlab/acme/repo/-/merge_requests/7") == ("gitlab/acme/repo", 7)

    def test_parse_merge_request_url_prefix_mismatch_keeps_current_behavior(self):
        provider = self._provider("https://gitlab.com")
        assert provider._parse_merge_request_url(
            "https://host.local/gitlab/shai/pr-agent/-/merge_requests/12") == ("gitlab/shai/pr-agent", 12)

    def test_parse_merge_request_url_rejects_non_mr_url_with_sub_path(self):
        provider = self._provider("https://host.local/gitlab")
        with pytest.raises(ValueError):
            provider._parse_merge_request_url("https://host.local/gitlab/shai/pr-agent")
