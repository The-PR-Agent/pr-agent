from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pr_agent.git_providers.gerrit_provider import GerritProvider, add_suggestion, adopt_to_gerrit_message, upload_patch
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

GERRIT_SETTINGS_KEYS = (
    "gerrit.url",
    "gerrit.user",
    "gerrit.patch_server_endpoint",
    "gerrit.patch_server_token",
)


@pytest.fixture
def gerrit_settings():
    """Snapshot the gerrit settings section and restore it after the test."""
    from pr_agent.config_loader import get_settings

    snapshot = snapshot_settings(GERRIT_SETTINGS_KEYS)
    yield get_settings()
    restore_settings(snapshot)


class TestAdoptToGerritMessage:
    def test_removes_markdown_bold_and_details_tags(self):
        message = "**PR Review**\n<details><summary>More info</summary>\nsome ``code`` here\n</details>"
        result = adopt_to_gerrit_message(message)
        assert "*" not in result
        assert "<details>" not in result
        assert "</details>" not in result
        assert "<summary>" not in result
        assert "</summary>" not in result
        assert "some `code` here" in result

    def test_header_is_converted_to_colon_terminated_line(self):
        result = adopt_to_gerrit_message("## PR Review:")
        assert result == "PR Review:"

    def test_header_without_colon_gets_colon_appended(self):
        result = adopt_to_gerrit_message("# Summary")
        assert result == "Summary:"

    def test_bullet_prefix_is_stripped(self):
        result = adopt_to_gerrit_message("- first item\n- second item")
        assert result == "first item\nsecond item"

    def test_mixed_message(self):
        message = "## Review:\n- **Score**: 85\nplain line"
        result = adopt_to_gerrit_message(message)
        assert result == "Review:\nScore: 85\nplain line"


class TestSplitSuggestion:
    def _provider(self):
        return GerritProvider.__new__(GerritProvider)

    def test_splits_description_and_code_context(self):
        provider = self._provider()
        msg = ("**Use a better name**\n"
               "```suggestion\n"
               "def better_name():\n"
               "    pass\n"
               "```")
        description, context = provider.split_suggestion(msg)
        assert description == "Use a better name"
        assert context == "def better_name():\n    pass\n"

    def test_no_code_block_yields_empty_context(self):
        provider = self._provider()
        description, context = provider.split_suggestion("just a description")
        assert description == "just a description"
        assert context == ""


class TestIsSupported:
    @pytest.mark.parametrize("capability", [
        "create_inline_comment",
        "publish_inline_comments",
        "get_labels",
        "gfm_markdown",
    ])
    def test_unsupported_capabilities(self, capability):
        provider = GerritProvider.__new__(GerritProvider)
        assert provider._is_supported(capability) is False

    @pytest.mark.parametrize("capability", [
        "get_issue_comments",
        "publish_description",
        "anything_else",
    ])
    def test_supported_capabilities(self, capability):
        provider = GerritProvider.__new__(GerritProvider)
        assert provider._is_supported(capability) is True


class TestGerritProviderInit:
    def _make_repo_mock(self):
        repo = MagicMock()
        branch = MagicMock()
        branch.name = "feature/my-change"
        repo.branches = [branch]
        repo.head.commit.diff.return_value = []
        return repo

    def test_init_parses_key_and_builds_authenticated_url(self, gerrit_settings):
        gerrit_settings.set("gerrit.url", "ssh://gerrit.example.com:29418")
        gerrit_settings.set("gerrit.user", "reviewer")
        repo_mock = self._make_repo_mock()

        with patch("pr_agent.git_providers.gerrit_provider.prepare_repo") as prepare_repo_mock, \
                patch("pr_agent.git_providers.gerrit_provider.Repo", return_value=repo_mock):
            prepare_repo_mock.return_value = Path("/tmp/fake-clone")
            provider = GerritProvider("my/project:refs/changes/01/1/1")

        assert provider.project == "my/project"
        assert provider.refspec == "refs/changes/01/1/1"
        assert provider.parsed_url.scheme == "ssh"
        assert provider.parsed_url.auth == "reviewer"
        assert provider.parsed_url.host == "gerrit.example.com"
        assert provider.parsed_url.port == 29418
        assert provider.pr_url == "ssh://gerrit.example.com:29418"
        prepare_repo_mock.assert_called_once_with(
            provider.parsed_url, "my/project", "refs/changes/01/1/1"
        )
        # PullRequestMimic is built from the branch name and (empty) diff
        assert provider.pr.title == "feature/my-change"
        assert provider.pr.diff_files == []

    def test_init_rejects_key_without_refspec_separator(self, gerrit_settings):
        with pytest.raises(ValueError):
            GerritProvider("project-without-refspec")

    def test_init_rejects_empty_project(self, gerrit_settings):
        with pytest.raises(AssertionError):
            GerritProvider(":refs/changes/01/1/1")

    def test_init_rejects_empty_refspec(self, gerrit_settings):
        with pytest.raises(AssertionError):
            GerritProvider("my/project:")

    def test_init_requires_gerrit_url_setting(self, gerrit_settings):
        gerrit_settings.set("gerrit.url", "")
        gerrit_settings.set("gerrit.user", "reviewer")
        with pytest.raises(AssertionError):
            GerritProvider("my/project:refs/changes/01/1/1")

    def test_init_requires_gerrit_user_setting(self, gerrit_settings):
        gerrit_settings.set("gerrit.url", "ssh://gerrit.example.com:29418")
        gerrit_settings.set("gerrit.user", "")
        with pytest.raises(AssertionError):
            GerritProvider("my/project:refs/changes/01/1/1")


class TestUploadPatch:
    def test_upload_patch_posts_to_server_and_returns_full_url(self, gerrit_settings):
        gerrit_settings.set("gerrit.patch_server_endpoint", "https://patch.example.com/")
        gerrit_settings.set("gerrit.patch_server_token", "secret-token")
        response = MagicMock()

        with patch("pr_agent.git_providers.gerrit_provider.requests.post",
                   return_value=response) as post_mock:
            result = upload_patch("some-patch-content", "codium-ai/refs/1/abcd")

        assert result == "https://patch.example.com/codium-ai/refs/1/abcd"
        response.raise_for_status.assert_called_once()
        args, kwargs = post_mock.call_args
        assert args[0] == "https://patch.example.com/"
        assert kwargs["json"] == {"content": "some-patch-content", "path": "codium-ai/refs/1/abcd"}
        assert kwargs["headers"]["Authorization"] == "Bearer secret-token"


class TestAddSuggestion:
    def test_replaces_line_range_with_context(self, tmp_path):
        src = tmp_path / "code.py"
        src.write_text("line1\nline2\nline3\nline4\nline5\n")

        add_suggestion(str(src), "replacement\n", 2, 3)

        assert src.read_text() == "line1\nreplacement\nline4\nline5\n"

    def test_empty_context_deletes_line_range(self, tmp_path):
        src = tmp_path / "code.py"
        src.write_text("line1\nline2\nline3\n")

        add_suggestion(str(src), "", 2, 2)

        assert src.read_text() == "line1\nline3\n"


class TestRepoSettingsAndComments:
    def _provider_with_repo_path(self, repo_path):
        provider = GerritProvider.__new__(GerritProvider)
        provider.repo_path = Path(repo_path)
        return provider

    def test_get_repo_settings_reads_pr_agent_toml(self, tmp_path):
        (tmp_path / ".pr_agent.toml").write_bytes(b"[config]\nmodel = 'x'\n")
        provider = self._provider_with_repo_path(tmp_path)
        assert provider.get_repo_settings() == b"[config]\nmodel = 'x'\n"

    def test_get_repo_settings_returns_empty_bytes_when_missing(self, tmp_path):
        provider = self._provider_with_repo_path(tmp_path)
        assert provider.get_repo_settings() == b""

    def test_publish_comment_adopts_message_and_sends(self):
        provider = GerritProvider.__new__(GerritProvider)
        provider.parsed_url = MagicMock()
        provider.refspec = "refs/changes/01/1/1"

        with patch("pr_agent.git_providers.gerrit_provider.add_comment") as add_comment_mock:
            provider.publish_comment("- **item one**")

        add_comment_mock.assert_called_once_with(
            provider.parsed_url, "refs/changes/01/1/1", "item one"
        )

    def test_publish_comment_skips_temporary_comments(self):
        provider = GerritProvider.__new__(GerritProvider)
        provider.parsed_url = MagicMock()
        provider.refspec = "refs/changes/01/1/1"

        with patch("pr_agent.git_providers.gerrit_provider.add_comment") as add_comment_mock:
            provider.publish_comment("please wait ...", is_temporary=True)

        add_comment_mock.assert_not_called()

    def test_publish_description_prepends_title(self):
        provider = GerritProvider.__new__(GerritProvider)
        provider.parsed_url = MagicMock()
        provider.refspec = "refs/changes/01/1/1"

        with patch("pr_agent.git_providers.gerrit_provider.add_comment") as add_comment_mock:
            provider.publish_description("My Title", "- body line")

        add_comment_mock.assert_called_once_with(
            provider.parsed_url, "refs/changes/01/1/1", "My Title\nbody line"
        )

    @pytest.mark.parametrize("method, args", [
        ("publish_inline_comment", ("body", "file.py", "line")),
        ("publish_inline_comments", ([{"body": "x"}],)),
        ("get_pr_labels", ()),
        ("add_eyes_reaction", (1,)),
        ("remove_reaction", (1, 2)),
    ])
    def test_unimplemented_methods_raise(self, method, args):
        provider = GerritProvider.__new__(GerritProvider)
        with pytest.raises(NotImplementedError):
            getattr(provider, method)(*args)
