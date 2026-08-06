from unittest.mock import MagicMock, patch

import pytest
from gitlab import Gitlab
from gitlab.exceptions import GitlabGetError
from gitlab.v4.objects import ProjectFile, ProjectMergeRequest, ProjectMergeRequestManager

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
        mock_project.files.get.side_effect = GitlabGetError("404 Not Found")

        content = gitlab_provider.get_repo_file_content("AGENTS.md")

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
