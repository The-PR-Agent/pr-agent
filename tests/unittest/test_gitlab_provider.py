from unittest.mock import MagicMock, patch

import pytest
from gitlab import Gitlab
from gitlab.exceptions import GitlabGetError
from gitlab.v4.objects import Project, ProjectFile

from pr_agent.git_providers.gitlab_provider import GitLabProvider


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

    # ---- publish_labels / get_pr_labels tests ----

    def _prime_mr_for_labels(self, gitlab_provider, server_labels):
        """Install a mock MR with server_labels for label-publishing tests.

        _get_merge_request is patched to return a fresh MagicMock with the
        same label set, simulating a successful server refresh. spec=[...]
        keeps MagicMock from silently auto-creating add_labels /
        remove_labels attrs so tests can assert which side(s) of the diff
        were written (or were cleared) after publish_labels returns.
        """
        mr = MagicMock(spec=["labels", "save"])
        mr.labels = list(server_labels)
        gitlab_provider.mr = mr
        gitlab_provider._get_merge_request = MagicMock(return_value=mr)
        return mr

    def _capture_wire_payload_on_save(self, mr):
        """Capture add_labels / remove_labels at the moment save() is called.

        publish_labels deletes the transient diff attributes in a ``finally``
        block, so asserting on them *after* the call returns is meaningless
        (they will always be absent). This helper installs a save() side_effect
        that records the diff payload that was actually written to the wire,
        which is what we need to validate.
        """
        captured = {}

        def _record_then_succeed(*_a, **_kw):
            captured["add_labels"] = getattr(mr, "add_labels", None)
            captured["remove_labels"] = getattr(mr, "remove_labels", None)

        mr.save.side_effect = _record_then_succeed
        return captured

    def test_publish_labels_noop_when_sets_equal(self, gitlab_provider):
        mr = self._prime_mr_for_labels(gitlab_provider, ["bug", "review effort 3/5"])

        gitlab_provider.publish_labels(["bug", "review effort 3/5"])

        # No diff -> no save, no transient attributes touched.
        mr.save.assert_not_called()
        assert not hasattr(mr, "add_labels")
        assert not hasattr(mr, "remove_labels")

    def test_publish_labels_adds_only_missing(self, gitlab_provider):
        mr = self._prime_mr_for_labels(gitlab_provider, ["bug"])
        captured = self._capture_wire_payload_on_save(mr)

        gitlab_provider.publish_labels(["bug", "review effort 3/5"])

        assert mr.save.call_count == 1
        # Only the missing label is in the add diff; nothing is being
        # removed because every server label is still desired.
        assert captured["add_labels"] == "review effort 3/5"
        assert captured["remove_labels"] is None
        # Diff attrs are cleared on the way out.
        assert not hasattr(mr, "add_labels")
        assert not hasattr(mr, "remove_labels")

    def test_publish_labels_removes_stale_managed_labels(self, gitlab_provider):
        mr = self._prime_mr_for_labels(
            gitlab_provider, ["review effort 5/5", "Possible security concern"]
        )
        captured = self._capture_wire_payload_on_save(mr)

        # Caller wants to switch the managed labels to a fresh set.
        gitlab_provider.publish_labels(["review effort 2/5"])

        assert mr.save.call_count == 1
        # "review effort 2/5" is added; both prior managed labels are removed.
        # sorted() determinism is part of the contract so we can assert the
        # exact comma-separated payload sent on the wire.
        assert captured["add_labels"] == "review effort 2/5"
        assert captured["remove_labels"] == "Possible security concern,review effort 5/5"
        assert not hasattr(mr, "add_labels")
        assert not hasattr(mr, "remove_labels")

    def test_publish_labels_preserves_user_labels_outside_diff(self, gitlab_provider):
        # The bug this PR fixes: a user-added label outside the diff must
        # not be touched. With spec on the mock, ``mr.labels`` should remain
        # the exact list we primed it with (no full-array overwrite).
        mr = self._prime_mr_for_labels(
            gitlab_provider, ["area/backend", "review effort 3/5"]
        )
        captured = self._capture_wire_payload_on_save(mr)

        # Caller flipped the managed label only; ``area/backend`` stays.
        gitlab_provider.publish_labels(["area/backend", "review effort 4/5"])

        # Wire-level diff: only the managed label is updated.
        assert captured["add_labels"] == "review effort 4/5"
        assert captured["remove_labels"] == "review effort 3/5"
        # We wrote exactly one save and never reassigned ``mr.labels`` (the
        # pre-fix bug).
        assert mr.save.call_count == 1
        assert mr.labels == ["area/backend", "review effort 3/5"]
        # Diff attrs cleared on the way out.
        assert not hasattr(mr, "add_labels")
        assert not hasattr(mr, "remove_labels")

    def test_publish_labels_aborts_when_refresh_fails(self, gitlab_provider):
        # Pre-fix behavior would have proceeded against the cached snapshot,
        # potentially clobbering user labels. New strict behavior: abort the
        # publish and leave server state untouched.
        cached_mr = MagicMock(spec=["labels", "save"])
        cached_mr.labels = ["stale label that no longer reflects server"]
        gitlab_provider.mr = cached_mr
        gitlab_provider._get_merge_request = MagicMock(side_effect=RuntimeError("boom"))

        gitlab_provider.publish_labels(["review effort 3/5"])

        cached_mr.save.assert_not_called()

    def test_publish_labels_clears_diff_attrs_on_save_failure(self, gitlab_provider):
        # If ``self.mr.save()`` raises, the transient diff fields must still
        # be cleared so a later, unrelated save() (e.g. publish_description)
        # does not resend them.
        mr = self._prime_mr_for_labels(gitlab_provider, ["bug"])
        mr.save.side_effect = RuntimeError("network blip")

        gitlab_provider.publish_labels(["review effort 3/5"])  # adds + removes

        # publish_labels swallows the outer Exception by design; what matters
        # is that the transient attrs do not leak into the next save().
        assert not hasattr(mr, "add_labels")
        assert not hasattr(mr, "remove_labels")

    def test_get_pr_labels_no_update_returns_cached(self, gitlab_provider):
        cached_mr = MagicMock()
        cached_mr.labels = ["cached"]
        gitlab_provider.mr = cached_mr
        gitlab_provider._get_merge_request = MagicMock()

        result = gitlab_provider.get_pr_labels(update=False)

        assert result == ["cached"]
        gitlab_provider._get_merge_request.assert_not_called()

    def test_get_pr_labels_with_update_refreshes(self, gitlab_provider):
        cached_mr = MagicMock()
        cached_mr.labels = ["cached-stale"]
        fresh_mr = MagicMock()
        fresh_mr.labels = ["fresh-from-server"]
        gitlab_provider.mr = cached_mr
        gitlab_provider._get_merge_request = MagicMock(return_value=fresh_mr)

        result = gitlab_provider.get_pr_labels(update=True)

        assert result == ["fresh-from-server"]
        assert gitlab_provider.mr is fresh_mr

    def test_get_pr_labels_with_update_propagates_refresh_failure(self, gitlab_provider):
        # Strict policy: surface the refresh failure to the caller (which
        # wraps the call in a broader try/except), rather than silently
        # returning stale data that would corrupt the read-modify-write cycle.
        gitlab_provider.mr = MagicMock()
        gitlab_provider._get_merge_request = MagicMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            gitlab_provider.get_pr_labels(update=True)

    # ---- publish_managed_labels (atomic refresh+filter+diff) tests ----

    @staticmethod
    def _is_review_managed(label):
        if label is None:
            return False
        lowered = label.lower()
        return (lowered.startswith("review effort")
                or lowered.startswith("possible security concern"))

    def test_publish_managed_labels_refreshes_once(self, gitlab_provider):
        # The whole point of this method: a single refresh feeds both the
        # filter (managed vs user) and the diff (add/remove). Two refreshes
        # would re-introduce the cross-snapshot race the reviewer flagged.
        mr = self._prime_mr_for_labels(gitlab_provider, ["review effort 1/5", "area/backend"])
        self._capture_wire_payload_on_save(mr)

        gitlab_provider.publish_managed_labels(["review effort 3/5"], self._is_review_managed)

        gitlab_provider._get_merge_request.assert_called_once()

    def test_publish_managed_labels_preserves_user_labels(self, gitlab_provider):
        mr = self._prime_mr_for_labels(
            gitlab_provider, ["review effort 1/5", "area/backend", "team/security"]
        )
        captured = self._capture_wire_payload_on_save(mr)

        result = gitlab_provider.publish_managed_labels(
            ["review effort 3/5"], self._is_review_managed
        )

        # The returned "new labels" set is what would exist server-side after
        # the publish: the managed set plus every user label.
        assert sorted(result) == sorted([
            "review effort 3/5", "area/backend", "team/security"
        ])
        # Wire payload: only the managed labels are diffed. The user labels
        # are not in to_add (already present) and not in to_remove (not
        # managed, so filter kept them).
        assert captured["add_labels"] == "review effort 3/5"
        assert captured["remove_labels"] == "review effort 1/5"
        assert mr.save.call_count == 1

    def test_publish_managed_labels_noop_when_nothing_changes(self, gitlab_provider):
        # If the desired managed set already matches what's on the server,
        # publish_managed_labels returns None without calling save().
        mr = self._prime_mr_for_labels(
            gitlab_provider, ["review effort 3/5", "area/backend"]
        )

        result = gitlab_provider.publish_managed_labels(
            ["review effort 3/5"], self._is_review_managed
        )

        assert result is None
        mr.save.assert_not_called()
        assert not hasattr(mr, "add_labels")
        assert not hasattr(mr, "remove_labels")

    def test_publish_managed_labels_removes_managed_when_desired_empty(self, gitlab_provider):
        # /review may decide no managed labels apply this run (e.g. effort
        # disabled). The provider must remove every label classified as
        # managed and leave user labels alone.
        mr = self._prime_mr_for_labels(
            gitlab_provider,
            ["review effort 1/5", "Possible security concern", "area/backend"],
        )
        captured = self._capture_wire_payload_on_save(mr)

        result = gitlab_provider.publish_managed_labels([], self._is_review_managed)

        assert sorted(result) == ["area/backend"]
        assert captured["add_labels"] is None
        assert captured["remove_labels"] == "Possible security concern,review effort 1/5"
        assert mr.save.call_count == 1

    def test_publish_managed_labels_aborts_when_refresh_fails(self, gitlab_provider):
        # Strict policy: a stale snapshot would produce an incorrect diff and
        # could clobber user labels. Abort and return None instead.
        cached_mr = MagicMock(spec=["labels", "save"])
        cached_mr.labels = ["stale"]
        gitlab_provider.mr = cached_mr
        gitlab_provider._get_merge_request = MagicMock(side_effect=RuntimeError("boom"))

        result = gitlab_provider.publish_managed_labels(
            ["review effort 3/5"], self._is_review_managed
        )

        assert result is None
        cached_mr.save.assert_not_called()

    def test_publish_managed_labels_clears_diff_attrs_on_save_failure(self, gitlab_provider):
        # The transient add_labels / remove_labels must never leak into a
        # later unrelated mr.save() (e.g. publish_description).
        mr = self._prime_mr_for_labels(gitlab_provider, ["review effort 1/5"])
        mr.save.side_effect = RuntimeError("network blip")

        gitlab_provider.publish_managed_labels(
            ["review effort 3/5"], self._is_review_managed
        )

        assert not hasattr(mr, "add_labels")
        assert not hasattr(mr, "remove_labels")
