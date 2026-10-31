"""The incremental scope set by '/review -i' must not survive the auto-command boundary.

gitlab_webhook.py calls reset_diff_cache_for_command() between the auto commands of
one webhook. The base-class reset (#4372) drops the whole diff cache and the
incremental anchor, so a command loop like push_commands = ["/review -i", "/improve"]
serves the second command the full MR diff, not the incremental subset the previous
command left behind.
"""

from unittest.mock import MagicMock, patch

import pytest
from gitlab import Gitlab

from pr_agent.git_providers.git_provider import IncrementalPR
from pr_agent.git_providers.gitlab_provider import GitLabProvider


def _change(path):
    return {
        "old_path": path, "new_path": path, "diff": "@@ -1 +1 @@\n-old\n+new\n",
        "new_file": False, "deleted_file": False, "renamed_file": False,
    }


class TestStaleIncrementalScopeAcrossCommands:
    @pytest.fixture
    def mock_gitlab_client(self):
        return MagicMock()

    @pytest.fixture
    def mock_project(self):
        return MagicMock()

    @pytest.fixture
    def provider(self, mock_gitlab_client, mock_project):
        with patch("pr_agent.git_providers.gitlab_provider.gitlab.Gitlab", return_value=mock_gitlab_client), \
             patch("pr_agent.git_providers.gitlab_provider.get_settings") as mock_settings:
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
            provider._own_user_id = None

            # Full MR: three files. The new commits (c1, c2) touch only a.py and b.py.
            provider._get_merge_request_changes = MagicMock(return_value={
                "changes": [_change("a.py"), _change("b.py"), _change("c.py")],
                "diff_refs": dict(provider.mr.diff_refs),
            })
            provider._expand_submodule_changes = MagicMock(side_effect=lambda changes, refs: changes)

            # File contents resolve for every ref.
            file_obj = MagicMock()
            file_obj.decode.return_value = b"line\n"
            mock_project.files.get.return_value = file_obj

            def _current_mr_metadata(*_args, **_kwargs):
                return {"sha": "current-mr", "diff_refs": dict(provider.mr.diff_refs), "changes_count": "3"}

            mock_gitlab_client.http_get.side_effect = _current_mr_metadata
            return provider

    @staticmethod
    def _make_note(note_id, body, created_at):
        note = MagicMock()
        note.id = note_id
        note.body = body
        note.created_at = created_at
        note.updated_at = created_at
        return note

    @staticmethod
    def _make_commit(sha, committed_date):
        commit = MagicMock(spec=["id", "committed_date", "authored_date", "created_at"])
        commit.id = sha
        commit.committed_date = committed_date
        commit.authored_date = committed_date
        commit.created_at = committed_date
        return commit

    def _anchor_incremental_on_new_commits(self, provider):
        """The setup '/review -i' performs: a prior review note at 10:00, then c1/c2."""
        provider.mr.notes.list.return_value = [
            self._make_note(7, "## PR Reviewer Guide 🔍\nbody", "2024-05-01T10:00:00Z"),
        ]
        provider.mr.commits.return_value = [
            self._make_commit("c2", "2024-05-01T11:30:00Z"),
            self._make_commit("c1", "2024-05-01T11:00:00Z"),
            self._make_commit("c0", "2024-05-01T09:00:00Z"),
        ]
        # repository_compare goes through a real python-gitlab project object; satisfy it
        # at the HTTP layer like the existing incremental tests do.
        client = Gitlab("https://gitlab.example.com", private_token="test-token")
        client.http_get = MagicMock(side_effect=lambda path, **kwargs: (
            {"diffs": [_change("a.py"), _change("b.py")]}
            if str(path).endswith("/repository/compare") else {"id": 1}
        ))
        provider.gl = client

    def test_full_diff_ground_truth(self, provider):
        """Without incremental state, get_diff_files returns the whole MR."""
        files = provider.get_diff_files()
        assert [f.filename for f in files] == ["a.py", "b.py", "c.py"]

    def test_incremental_scope_does_not_survive_the_command_boundary(self, provider):
        # ---- Command 1: '/review -i' (pr_reviewer.py:205-206) ----
        self._anchor_incremental_on_new_commits(provider)
        provider.get_incremental_commits(IncrementalPR(True))
        incremental_files = provider.get_diff_files()
        assert [f.filename for f in incremental_files] == ["a.py", "b.py"]
        assert provider.diff_files is not None and len(provider.diff_files) == 2

        # ---- The auto-command boundary, exactly as gitlab_webhook.py runs it ----
        reset = getattr(provider, "reset_diff_cache_for_command", None)
        assert callable(reset)
        reset()

        # The boundary reset drops the whole cache — a non-empty one included — and
        # the incremental anchor with it (#4372).
        assert provider.diff_files is None
        assert provider.incremental.is_incremental is False

        # ---- Command 2: '/improve' without -i ----
        # pr_code_suggestions.py guards get_incremental_commits behind the command's
        # OWN flag, so the tool only calls get_diff_files(); it must see the full MR.
        second_command_files = provider.get_diff_files()
        assert [f.filename for f in second_command_files] == ["a.py", "b.py", "c.py"]
