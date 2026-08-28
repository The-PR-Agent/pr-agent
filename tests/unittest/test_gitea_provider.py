from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from giteapy.rest import ApiException

from pr_agent.git_providers.gitea_provider import GiteaProvider


class TestGiteaProvider:
    @patch("pr_agent.git_providers.gitea_provider.giteapy.ApiClient")
    @patch("pr_agent.git_providers.gitea_provider.get_settings")
    def test_diff_cache_starts_unloaded(self, mock_get_settings, _):
        mock_get_settings.return_value.get.side_effect = lambda key, default=None: {
            "GITEA.PERSONAL_ACCESS_TOKEN": "token",
        }.get(key, default)

        provider = GiteaProvider("https://gitea.example.com/repository")

        assert provider.diff_files is None

    @patch('pr_agent.git_providers.gitea_provider.get_settings')
    @patch('pr_agent.git_providers.gitea_provider.giteapy.ApiClient')
    def test_gitea_provider_auth_header(self, mock_api_client_cls, mock_get_settings):
        # Setup settings
        settings = MagicMock()
        settings.get.side_effect = lambda k, d=None: {
            'GITEA.URL': 'https://gitea.example.com',
            'GITEA.PERSONAL_ACCESS_TOKEN': 'test-token',
            'GITEA.REPO_SETTING': None,
            'GITEA.SKIP_SSL_VERIFICATION': False,
            'GITEA.SSL_CA_CERT': None
        }.get(k, d)
        mock_get_settings.return_value = settings

        # Setup ApiClient mock
        mock_api_client = mock_api_client_cls.return_value
        # Mock configuration object on client
        mock_api_client.configuration.api_key = {'Authorization': 'token test-token'}

        # Mock responses for calls made during initialization
        def call_api_side_effect(path, method, **kwargs):
            mock_resp = MagicMock()
            if 'files' in path: # get_change_file_pull_request
                mock_resp.data = BytesIO(b'[]')
                return mock_resp
            if 'commits' in path:
                mock_resp.data = BytesIO(b'[]')
                return mock_resp

            # Default fallback
            mock_resp.data = BytesIO(b'{}')
            return mock_resp

        mock_api_client.call_api.side_effect = call_api_side_effect

        from pr_agent.git_providers.gitea_provider import RepoApi

        client = mock_api_client
        repo_api = RepoApi(client)

        # Now test methods independently

        # 1. get_change_file_pull_request
        mock_api_client.reset_mock()
        mock_resp = MagicMock()
        mock_resp.data = BytesIO(b'[]')
        mock_api_client.call_api.return_value = mock_resp

        repo_api.get_change_file_pull_request('owner', 'repo', 123)

        args, kwargs = mock_api_client.call_api.call_args
        assert '/repos/owner/repo/pulls/123/files' in args[0]
        assert kwargs.get('auth_settings') == ['AuthorizationHeaderToken']
        assert 'token=' not in args[0]

        # 2. get_pull_request_diff
        mock_api_client.reset_mock()
        mock_resp = MagicMock()
        mock_resp.data = BytesIO(b'diff content')
        mock_api_client.call_api.return_value = mock_resp

        repo_api.get_pull_request_diff('owner', 'repo', 123)

        args, kwargs = mock_api_client.call_api.call_args
        assert args[0] == '/repos/owner/repo/pulls/123.diff'
        assert kwargs.get('auth_settings') == ['AuthorizationHeaderToken']

        # 3. get_languages
        mock_api_client.reset_mock()
        mock_resp.data = BytesIO(b'{"Python": 100}')
        mock_api_client.call_api.return_value = mock_resp

        repo_api.get_languages('owner', 'repo')

        args, kwargs = mock_api_client.call_api.call_args
        assert args[0] == '/repos/owner/repo/languages'
        assert kwargs.get('auth_settings') == ['AuthorizationHeaderToken']

        # 4. get_file_content
        mock_api_client.reset_mock()
        mock_resp.data = BytesIO(b'content')
        mock_api_client.call_api.return_value = mock_resp

        repo_api.get_file_content('owner', 'repo', 'sha1', 'file.txt')

        args, kwargs = mock_api_client.call_api.call_args
        assert args[0] == '/repos/owner/repo/raw/file.txt'
        assert kwargs.get('query_params') == [('ref', 'sha1')]
        assert kwargs.get('auth_settings') == ['AuthorizationHeaderToken']

        # 5. get_pr_commits
        mock_api_client.reset_mock()
        mock_resp.data = BytesIO(b'[]')
        mock_api_client.call_api.return_value = mock_resp

        repo_api.get_pr_commits('owner', 'repo', 123)

        args, kwargs = mock_api_client.call_api.call_args
        assert args[0] == '/repos/owner/repo/pulls/123/commits'
        assert kwargs.get('auth_settings') == ['AuthorizationHeaderToken']


    @patch('pr_agent.git_providers.gitea_provider.get_settings')
    @patch('pr_agent.git_providers.gitea_provider.giteapy.ApiClient')
    def test_gitea_provider_preserves_non_utf8_text_file_content(self, mock_api_client_cls, mock_get_settings):
        # Regression for the Qodo review on #2440: non-UTF-8 *text* (e.g. UTF-16)
        # must not be dropped to "" (which is indistinguishable from an empty file
        # and loses real content downstream). It is decoded via the shared
        # decode_if_bytes fallback chain instead of crashing or returning "".
        settings = MagicMock()
        settings.get.side_effect = lambda k, d=None: {
            'GITEA.URL': 'https://gitea.example.com',
            'GITEA.PERSONAL_ACCESS_TOKEN': 'test-token',
            'GITEA.REPO_SETTING': None,
            'GITEA.SKIP_SSL_VERIFICATION': False,
            'GITEA.SSL_CA_CERT': None
        }.get(k, d)
        mock_get_settings.return_value = settings

        mock_api_client = mock_api_client_cls.return_value
        mock_api_client.configuration.api_key = {'Authorization': 'token test-token'}
        mock_resp = MagicMock()
        # UTF-16-LE encoded text — not valid UTF-8, but legitimate text content.
        mock_resp.data = BytesIO("hello world".encode("utf-16"))
        mock_api_client.call_api.return_value = mock_resp

        from pr_agent.git_providers.gitea_provider import RepoApi

        repo_api = RepoApi(mock_api_client)

        content = repo_api.get_file_content('owner', 'repo', 'sha1', 'notes.txt')
        assert content != '', "non-UTF-8 text must not be dropped to an empty string"
        assert all(ch in content for ch in "hello world"), "the underlying text should survive the fallback decode"
        args, kwargs = mock_api_client.call_api.call_args
        assert args[0] == '/repos/owner/repo/raw/notes.txt'
        assert kwargs.get('query_params') == [('ref', 'sha1')]
        assert kwargs.get('auth_settings') == ['AuthorizationHeaderToken']

    @patch('pr_agent.git_providers.gitea_provider.get_settings')
    @patch('pr_agent.git_providers.gitea_provider.giteapy.ApiClient')
    def test_gitea_provider_does_not_crash_on_binary_file_content(self, mock_api_client_cls, mock_get_settings):
        # The original #2380 crash path: raw binary bytes must not raise
        # UnicodeDecodeError. decode_if_bytes yields a best-effort string; binary
        # files are filtered downstream by extension, so this only needs to not crash.
        settings = MagicMock()
        settings.get.side_effect = lambda k, d=None: {
            'GITEA.URL': 'https://gitea.example.com',
            'GITEA.PERSONAL_ACCESS_TOKEN': 'test-token',
            'GITEA.REPO_SETTING': None,
            'GITEA.SKIP_SSL_VERIFICATION': False,
            'GITEA.SSL_CA_CERT': None
        }.get(k, d)
        mock_get_settings.return_value = settings

        mock_api_client = mock_api_client_cls.return_value
        mock_api_client.configuration.api_key = {'Authorization': 'token test-token'}
        mock_resp = MagicMock()
        mock_resp.data = BytesIO(b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01')  # JPEG header bytes
        mock_api_client.call_api.return_value = mock_resp

        from pr_agent.git_providers.gitea_provider import RepoApi

        repo_api = RepoApi(mock_api_client)

        # Must not raise; result is a string (content filtered by extension downstream).
        assert isinstance(repo_api.get_file_content('owner', 'repo', 'sha1', 'assets/image.webp'), str)


    @patch('pr_agent.git_providers.gitea_provider.get_settings')
    @patch('pr_agent.git_providers.gitea_provider.giteapy.ApiClient')
    def test_gitea_provider_decodes_non_utf8_diff_with_replacement(self, mock_api_client_cls, mock_get_settings):
        settings = MagicMock()
        settings.get.side_effect = lambda k, d=None: {
            'GITEA.URL': 'https://gitea.example.com',
            'GITEA.PERSONAL_ACCESS_TOKEN': 'test-token',
            'GITEA.REPO_SETTING': None,
            'GITEA.SKIP_SSL_VERIFICATION': False,
            'GITEA.SSL_CA_CERT': None
        }.get(k, d)
        mock_get_settings.return_value = settings

        mock_api_client = mock_api_client_cls.return_value
        mock_api_client.configuration.api_key = {'Authorization': 'token test-token'}
        mock_resp = MagicMock()
        mock_resp.data = BytesIO(b'diff --git a/image.png b/image.webp\n+' + bytes([0xff]) + b'binary')
        mock_api_client.call_api.return_value = mock_resp

        from pr_agent.git_providers.gitea_provider import RepoApi

        repo_api = RepoApi(mock_api_client)

        diff = repo_api.get_pull_request_diff('owner', 'repo', 123)

        assert 'diff --git a/image.png b/image.webp' in diff
        assert '�' in diff
        args, kwargs = mock_api_client.call_api.call_args
        assert args[0] == '/repos/owner/repo/pulls/123.diff'
        assert kwargs.get('auth_settings') == ['AuthorizationHeaderToken']
    def test_get_repo_settings_returns_bytes(self):
        """Regression for #2347: get_repo_settings must return bytes so that
        utils.apply_repo_settings can os.write() it and later .decode() it. The
        Gitea raw-file API yields str (unlike GitHub/GitLab/Bitbucket, which hand
        back bytes), so the provider must encode before returning."""
        from pr_agent.git_providers.gitea_provider import GiteaProvider

        toml = '[pr_reviewer]\nnum_code_suggestions = 4\n'
        provider = GiteaProvider.__new__(GiteaProvider)
        provider.logger = MagicMock()
        provider.owner = 'owner'
        provider.repo = 'repo'
        provider.sha = 'sha1'
        provider.repo_settings = '.pr_agent.toml'
        provider.repo_api = MagicMock()
        provider.repo_api.get_file_content.return_value = toml  # API decodes to str

        result = provider.get_repo_settings()

        assert isinstance(result, bytes)
        assert result == toml.encode('utf-8')
        # The bytes must survive the exact operations utils.py performs on them.
        assert result.decode() == toml

    def test_get_repo_settings_empty_bytes_when_unset_or_missing(self):
        """No settings path configured, or empty/absent file: return empty
        bytes, so every code path honours the -> bytes contract (not just the
        success path) and a caller can never receive a str."""
        from pr_agent.git_providers.gitea_provider import GiteaProvider

        unset = GiteaProvider.__new__(GiteaProvider)
        unset.logger = MagicMock()
        unset.repo_settings = None
        assert unset.get_repo_settings() == b""

        empty = GiteaProvider.__new__(GiteaProvider)
        empty.logger = MagicMock()
        empty.owner = 'owner'
        empty.repo = 'repo'
        empty.sha = 'sha1'
        empty.repo_settings = '.pr_agent.toml'
        empty.repo_api = MagicMock()
        empty.repo_api.get_file_content.return_value = ''
        assert empty.get_repo_settings() == b""

    def test_get_repo_file_content_loads_from_base_sha(self):
        provider = GiteaProvider.__new__(GiteaProvider)
        provider.owner = "owner"
        provider.repo = "repo"
        provider.sha = "head-sha"
        provider.base_sha = "base-sha"
        provider.base_ref = "main"
        provider.logger = MagicMock()
        provider.repo_api = MagicMock()
        provider.repo_api.get_file_content.return_value = "repo context"

        content = provider.get_repo_file_content("AGENTS.md")

        assert content == "repo context"
        provider.repo_api.get_file_content.assert_called_once_with(
            owner="owner",
            repo="repo",
            commit_sha="base-sha",
            filepath="AGENTS.md"
        )

    def test_get_repo_file_content_loads_from_base_ref_when_base_sha_missing(self):
        provider = GiteaProvider.__new__(GiteaProvider)
        provider.owner = "owner"
        provider.repo = "repo"
        provider.sha = "head-sha"
        provider.base_sha = ""
        provider.base_ref = "main"
        provider.logger = MagicMock()
        provider.repo_api = MagicMock()
        provider.repo_api.get_file_content.return_value = "repo context"

        content = provider.get_repo_file_content("AGENTS.md")

        assert content == "repo context"
        provider.repo_api.get_file_content.assert_called_once_with(
            owner="owner",
            repo="repo",
            commit_sha="main",
            filepath="AGENTS.md"
        )

    def test_get_repo_file_content_from_default_branch(self):
        provider = GiteaProvider.__new__(GiteaProvider)
        provider.owner = "owner"
        provider.repo = "repo"
        provider.base_sha = "base-sha"
        provider.base_ref = "release-1.0"
        provider.sha = "head-sha"
        provider.logger = MagicMock()
        provider.repo_api = MagicMock()
        provider.repo_api.repo_get.return_value = MagicMock(default_branch="main")
        provider.repo_api.get_file_content.return_value = "repo context"

        content = provider.get_repo_file_content("AGENTS.md", from_default_branch=True)

        assert content == "repo context"
        provider.repo_api.get_file_content.assert_called_once_with(
            owner="owner",
            repo="repo",
            commit_sha="main",
            filepath="AGENTS.md"
        )

    def test_get_repo_file_content_treats_404_as_missing(self):
        provider = GiteaProvider.__new__(GiteaProvider)
        provider.owner = "owner"
        provider.repo = "repo"
        provider.base_sha = "base-sha"
        provider.base_ref = "main"
        provider.logger = MagicMock()
        provider.repo_api = MagicMock()
        provider.repo_api.get_file_content.side_effect = ApiException(status=404)

        assert provider.get_repo_file_content("MISSING.md") == ""

    def test_get_repo_file_content_propagates_transient_error(self):
        # Transient/unexpected errors must propagate so the repo-context loader flags a fetch
        # error and does not cache an empty result.
        provider = GiteaProvider.__new__(GiteaProvider)
        provider.owner = "owner"
        provider.repo = "repo"
        provider.base_sha = "base-sha"
        provider.base_ref = "main"
        provider.logger = MagicMock()
        provider.repo_api = MagicMock()
        provider.repo_api.get_file_content.side_effect = ApiException(status=500)

        with pytest.raises(ApiException):
            provider.get_repo_file_content("AGENTS.md")

    def test_get_repo_file_content_never_reads_from_pr_head_when_base_missing(self):
        # Security: when no target/base ref is available, the provider must NOT fall back
        # to the PR head (self.sha) — otherwise a PR could supply its own instruction files.
        provider = GiteaProvider.__new__(GiteaProvider)
        provider.owner = "owner"
        provider.repo = "repo"
        provider.sha = "head-sha"
        provider.base_sha = ""
        provider.base_ref = ""
        provider.logger = MagicMock()
        provider.repo_api = MagicMock()
        provider.repo_api.get_file_content.return_value = "repo context"

        content = provider.get_repo_file_content("AGENTS.md")

        assert content == ""
        provider.repo_api.get_file_content.assert_not_called()


class TestGiteaProviderPRCommits:
    """Regression tests for #2206: the provider must resolve ``last_commit`` from the
    commits of the PR, not from the commits of the repository's default branch.

    ``__init__`` performs network calls, so the instance is built with ``__new__`` and
    only the attributes ``_set_pr_commits`` uses are wired up.
    """

    @staticmethod
    def _provider(pr_commits, head_sha="head-sha"):
        from pr_agent.git_providers.gitea_provider import GiteaProvider

        provider = GiteaProvider.__new__(GiteaProvider)
        provider.logger = MagicMock()
        provider.owner = "owner"
        provider.repo = "repo"
        provider.pr_number = 123
        provider.sha = head_sha
        provider.repo_api = MagicMock()
        provider.repo_api.get_pr_commits.return_value = pr_commits
        return provider

    def test_commits_are_fetched_from_the_pr_not_from_the_repository(self):
        provider = self._provider([{"sha": "head-sha", "html_url": "url/head-sha"}])

        provider._set_pr_commits()

        provider.repo_api.get_pr_commits.assert_called_once_with(
            owner="owner",
            repo="repo",
            pr_number=123
        )
        provider.repo_api.list_all_commits.assert_not_called()

    def test_last_commit_is_the_pr_head_although_gitea_returns_newest_first(self):
        # Gitea returns PR commits newest-first, so the head commit comes first.
        provider = self._provider([
            {"sha": "head-sha", "html_url": "url/head-sha"},
            {"sha": "older-sha", "html_url": "url/older-sha"},
        ])

        provider._set_pr_commits()

        assert provider.last_commit.sha == "head-sha"
        assert provider.last_commit_id is provider.last_commit
        assert provider.get_latest_commit_url() == "url/head-sha"
        # Stored oldest-first, matching the iteration order of the other providers.
        assert [commit.sha for commit in provider.pr_commits] == ["older-sha", "head-sha"]

    def test_last_commit_falls_back_to_newest_commit_when_head_sha_is_unknown(self):
        provider = self._provider([
            {"sha": "newest-sha", "html_url": "url/newest-sha"},
            {"sha": "older-sha", "html_url": "url/older-sha"},
        ], head_sha="")

        provider._set_pr_commits()

        assert provider.last_commit.sha == "newest-sha"

    def test_no_commits_falls_back_to_a_commit_wrapping_the_head_sha(self):
        provider = self._provider([])

        provider._set_pr_commits()

        assert provider.pr_commits == []
        # last_commit must stay a valid object (never None) so callers that dereference
        # last_commit.sha / last_commit_id.sha don't crash, and get a real SHA instead of "".
        assert provider.last_commit is not None
        assert provider.last_commit.sha == "head-sha"
        assert provider.last_commit_id is provider.last_commit
        assert provider.get_latest_commit_url() == ""
        provider.logger.error.assert_called_once()

    def test_non_list_commits_payload_is_treated_as_empty(self):
        provider = self._provider({"message": "not found"})

        provider._set_pr_commits()

        assert provider.pr_commits == []
        assert provider.last_commit.sha == "head-sha"
        provider.logger.error.assert_called()

    def test_file_content_is_read_from_the_pr_head_commit(self):
        provider = self._provider([{"sha": "head-sha", "html_url": "url/head-sha"}])
        provider.repo_api.get_file_content.return_value = "content"

        provider._set_pr_commits()
        provider._get_file_content_from_latest_commit("main.py")

        provider.repo_api.get_file_content.assert_called_once_with(
            owner="owner",
            repo="repo",
            commit_sha="head-sha",
            filepath="main.py"
        )


class TestGiteaProviderAddFileDiff:
    """Tests for GiteaProvider.__add_file_diff diff parsing.

    The provider parses the raw unified diff returned by Gitea into a
    ``{file_path: patch}`` mapping. These tests exercise that parsing in
    isolation, bypassing __init__ (which performs network calls) by building the
    instance with ``__new__`` and wiring up only the attributes the method uses.
    """

    @staticmethod
    def _parse_diff(diff_content):
        from pr_agent.git_providers.gitea_provider import GiteaProvider

        provider = GiteaProvider.__new__(GiteaProvider)
        provider.logger = MagicMock()
        provider.owner = 'owner'
        provider.repo = 'repo'
        provider.pr_number = 1
        provider.file_diffs = {}
        provider.repo_api = MagicMock()
        provider.repo_api.get_pull_request_diff.return_value = diff_content
        # Invoke the name-mangled private method.
        provider._GiteaProvider__add_file_diff()
        return provider.file_diffs

    def test_single_hunk_is_parsed(self):
        diff = (
            'diff --git a/file1.py b/file1.py\n'
            'index 1111111..2222222 100644\n'
            '--- a/file1.py\n'
            '+++ b/file1.py\n'
            '@@ -1,3 +1,4 @@\n'
            ' line1\n'
            '+added line\n'
            ' line2\n'
            ' line3'
        )
        expected = (
            '@@ -1,3 +1,4 @@\n'
            ' line1\n'
            '+added line\n'
            ' line2\n'
            ' line3'
        )
        assert self._parse_diff(diff) == {'file1.py': expected}

    def test_multi_hunk_diff_keeps_all_hunks(self):
        """Regression for multi-hunk diffs (#2137).

        The previous implementation reset ``current_patch`` on every ``@@`` line,
        so only the last hunk of a file survived. All hunks must be preserved.
        """
        diff = (
            'diff --git a/file1.py b/file1.py\n'
            'index 1111111..2222222 100644\n'
            '--- a/file1.py\n'
            '+++ b/file1.py\n'
            '@@ -1,3 +1,4 @@\n'
            ' line1\n'
            '+added line\n'
            ' line2\n'
            ' line3\n'
            '@@ -10,3 +11,4 @@\n'
            ' line10\n'
            '+another added\n'
            ' line11\n'
            ' line12'
        )
        expected = (
            '@@ -1,3 +1,4 @@\n'
            ' line1\n'
            '+added line\n'
            ' line2\n'
            ' line3\n'
            '@@ -10,3 +11,4 @@\n'
            ' line10\n'
            '+another added\n'
            ' line11\n'
            ' line12'
        )
        file_diffs = self._parse_diff(diff)
        assert file_diffs == {'file1.py': expected}
        # Both hunk headers must be present (the bug dropped the first one).
        assert file_diffs['file1.py'].count('@@ -') == 2

    def test_multiple_files_each_with_multiple_hunks(self):
        diff = (
            'diff --git a/file1.py b/file1.py\n'
            'index 1111111..2222222 100644\n'
            '--- a/file1.py\n'
            '+++ b/file1.py\n'
            '@@ -1,2 +1,3 @@\n'
            ' a\n'
            '+b\n'
            ' c\n'
            '@@ -20,2 +21,3 @@\n'
            ' d\n'
            '+e\n'
            ' f\n'
            'diff --git a/file2.py b/file2.py\n'
            'index 3333333..4444444 100644\n'
            '--- a/file2.py\n'
            '+++ b/file2.py\n'
            '@@ -5,2 +5,3 @@\n'
            ' g\n'
            '+h\n'
            ' i\n'
            '@@ -30,2 +31,3 @@\n'
            ' j\n'
            '+k\n'
            ' l'
        )
        file_diffs = self._parse_diff(diff)
        assert set(file_diffs.keys()) == {'file1.py', 'file2.py'}
        assert file_diffs['file1.py'].count('@@ -') == 2
        assert file_diffs['file2.py'].count('@@ -') == 2
        assert file_diffs['file1.py'].startswith('@@ -1,2 +1,3 @@')
        assert file_diffs['file2.py'].startswith('@@ -5,2 +5,3 @@')

    def test_empty_diff_results_in_no_patches(self):
        assert self._parse_diff('') == {}

    def test_api_error_is_swallowed_and_logged(self):
        from pr_agent.git_providers.gitea_provider import GiteaProvider

        provider = GiteaProvider.__new__(GiteaProvider)
        provider.logger = MagicMock()
        provider.owner = 'owner'
        provider.repo = 'repo'
        provider.pr_number = 1
        provider.file_diffs = {}
        provider.repo_api = MagicMock()
        provider.repo_api.get_pull_request_diff.side_effect = Exception('boom')

        provider._GiteaProvider__add_file_diff()

        provider.logger.error.assert_called_once()
        # file_diffs is left untouched when the diff cannot be fetched.
        assert provider.file_diffs == {}

    @patch("pr_agent.git_providers.gitea_provider.giteapy.RepositoryApi")
    def test_edit_pull_request_without_title(self, mock_repository_api_cls):
        from pr_agent.git_providers.gitea_provider import RepoApi

        repo_api = RepoApi(MagicMock())
        repo_api.edit_pull_request("owner", "repo", 123, "Updated description")

        mock_repository_api_cls.return_value.repo_edit_pull_request.assert_called_once_with(
            owner="owner",
            repo="repo",
            index=123,
            body={"body": "Updated description"}
        )

    @patch("pr_agent.git_providers.gitea_provider.giteapy.RepositoryApi")
    def test_edit_pull_request_with_title(self, mock_repository_api_cls):
        from pr_agent.git_providers.gitea_provider import RepoApi

        repo_api = RepoApi(MagicMock())
        repo_api.edit_pull_request("owner", "repo", 123, "Updated description", "Updated title")

        mock_repository_api_cls.return_value.repo_edit_pull_request.assert_called_once_with(
            owner="owner",
            repo="repo",
            index=123,
            body={"body": "Updated description", "title": "Updated title"}
        )


class TestGiteaProviderUserFacingLinks:
    """Regression tests for #2612: links published in comments must use the
    user-facing base URL, not the (possibly internal) API base URL.

    ``__init__`` performs network calls, so instances are built with ``__new__``
    and only the attributes under test are wired up. ``base_url`` below stands
    in for an internal address (e.g. a Docker service name) that users cannot
    browse. ``pr_url`` uses the user-facing form throughout: ``_parse_pr_url``
    rejects the API form, so a constructed provider could never carry it, and
    Gitea sets ``url``/``html_url`` on the PR payload to the user-facing page.
    """

    @staticmethod
    def _provider(pr_html_url="", pr_url="http://forgejo:3000/owner/repo/pulls/4", pr_number=4):
        from pr_agent.git_providers.gitea_provider import GiteaProvider

        provider = GiteaProvider.__new__(GiteaProvider)
        provider.logger = MagicMock()
        provider.owner = "owner"
        provider.repo = "repo"
        provider.pr_number = pr_number
        provider.base_url = "http://forgejo:3000"
        provider.pr_url = pr_url
        provider.pr = None if pr_html_url is None else MagicMock(html_url=pr_html_url)
        return provider

    @staticmethod
    def _settings(web_url="", configured_url=None):
        # configured_url=None leaves GITEA.URL unset, exercising the derivation path.
        settings = MagicMock()
        values = {"GITEA.WEB_URL": web_url}
        if configured_url is not None:
            values["GITEA.URL"] = configured_url
        settings.get.side_effect = lambda k, d=None: values.get(k, d)
        return settings

    @patch("pr_agent.git_providers.gitea_provider.get_settings")
    def test_web_url_setting_takes_precedence(self, mock_get_settings):
        mock_get_settings.return_value = self._settings(
            web_url="https://git.example.com/forgejo/", configured_url="http://forgejo:3000")
        provider = self._provider(pr_html_url="https://other.example.com/owner/repo/pulls/4")

        assert provider._resolve_base_url_html() == "https://git.example.com/forgejo"

    @patch("pr_agent.git_providers.gitea_provider.get_settings")
    def test_configured_url_preferred_over_pr_html_url(self, mock_get_settings):
        # An operator-set GITEA.URL wins over html_url, which the server builds from
        # its own ROOT_URL - still the (wrong) default on some instances.
        mock_get_settings.return_value = self._settings(configured_url="http://forgejo:3000")
        provider = self._provider(pr_html_url="http://localhost:3000/owner/repo/pulls/4")

        assert provider._resolve_base_url_html() == "http://forgejo:3000"

    @patch("pr_agent.git_providers.gitea_provider.get_settings")
    def test_shipped_default_url_does_not_shadow_pr_html_url(self, mock_get_settings):
        # configuration.toml always ships url = "https://gitea.com", so the default must not
        # count as operator-set - otherwise the html_url derivation is unreachable in
        # production and the #2612 scenario regresses (review feedback on #2617).
        mock_get_settings.return_value = self._settings(configured_url="https://gitea.com")
        provider = self._provider(pr_html_url="https://git.example.com/forgejo/owner/repo/pulls/4")

        assert provider._resolve_base_url_html() == "https://git.example.com/forgejo"

    @patch("pr_agent.git_providers.gitea_provider.get_settings")
    def test_shipped_default_url_with_trailing_slash_does_not_shadow(self, mock_get_settings):
        mock_get_settings.return_value = self._settings(configured_url="https://gitea.com/")
        provider = self._provider(pr_html_url="https://git.example.com/forgejo/owner/repo/pulls/4")

        assert provider._resolve_base_url_html() == "https://git.example.com/forgejo"

    @patch("pr_agent.git_providers.gitea_provider.get_settings")
    def test_derives_base_url_from_pr_html_url(self, mock_get_settings):
        # Zero-config case: Forgejo/Gitea builds html_url from its external ROOT_URL,
        # so the user-facing base URL can be derived even without a web_url setting.
        mock_get_settings.return_value = self._settings()
        provider = self._provider(pr_html_url="https://git.example.com/forgejo/owner/repo/pulls/4")

        assert provider._resolve_base_url_html() == "https://git.example.com/forgejo"

    @patch("pr_agent.git_providers.gitea_provider.get_settings")
    def test_falls_back_to_base_url_without_pr(self, mock_get_settings):
        # Issue flow has no PR object; existing single-URL deployments stay unchanged.
        mock_get_settings.return_value = self._settings()
        provider = self._provider(pr_html_url=None)

        assert provider._resolve_base_url_html() == "http://forgejo:3000"

    @patch("pr_agent.git_providers.gitea_provider.get_settings")
    def test_falls_back_to_base_url_when_html_url_missing_or_foreign(self, mock_get_settings):
        mock_get_settings.return_value = self._settings()

        assert self._provider(pr_html_url="")._resolve_base_url_html() == "http://forgejo:3000"
        foreign = self._provider(pr_html_url="https://ci.example.com/artifacts/owner/repo")
        assert foreign._resolve_base_url_html() == "http://forgejo:3000"

    @patch("pr_agent.git_providers.gitea_provider.get_settings")
    def test_get_line_link_uses_user_facing_base_url(self, mock_get_settings):
        mock_get_settings.return_value = self._settings(web_url="https://git.example.com/forgejo")
        provider = self._provider()
        # Mirror __init__ so the resolver wiring (self.base_url_html assignment) is covered too.
        provider.base_url_html = provider._resolve_base_url_html()
        provider.get_pr_branch = MagicMock(return_value="feat/retry")

        prefix = "https://git.example.com/forgejo/owner/repo/src/branch/feat/retry/app/storage.py"
        assert provider.get_line_link("app/storage.py", 24) == f"{prefix}#L24"
        assert provider.get_line_link("app/storage.py", 24, 30) == f"{prefix}#L24-L30"
        assert provider.get_line_link("app/storage.py", -1) == prefix

    @patch("pr_agent.git_providers.gitea_provider.get_settings")
    def test_get_pr_url_uses_user_facing_base_url(self, mock_get_settings):
        # html_url here comes from a stale ROOT_URL; the configured WEB_URL must win.
        mock_get_settings.return_value = self._settings(web_url="https://git.example.com/forgejo")
        provider = self._provider(pr_html_url="http://localhost:3000/owner/repo/pulls/4")
        provider.base_url_html = provider._resolve_base_url_html()

        assert provider.get_pr_url() == "https://git.example.com/forgejo/owner/repo/pulls/4"

    @patch("pr_agent.git_providers.gitea_provider.get_settings")
    def test_get_pr_url_uses_configured_base_url(self, mock_get_settings):
        mock_get_settings.return_value = self._settings(configured_url="http://forgejo:3000")
        provider = self._provider(pr_html_url="http://localhost:3000/owner/repo/pulls/4")
        provider.base_url_html = provider._resolve_base_url_html()

        assert provider.get_pr_url() == "http://forgejo:3000/owner/repo/pulls/4"

    @patch("pr_agent.git_providers.gitea_provider.get_settings")
    def test_get_pr_url_falls_back_to_raw_pr_url(self, mock_get_settings):
        # Issue flow has no PR number; get_pr_url returns the raw URL unchanged.
        mock_get_settings.return_value = self._settings()
        provider = self._provider(pr_html_url=None, pr_number=None)
        provider.base_url_html = provider._resolve_base_url_html()

        assert provider.get_pr_url() == "http://forgejo:3000/owner/repo/pulls/4"


class TestGiteaProviderUrlParsing:
    """Cover both URL forms: Gitea sets ``pull_request.url`` to the web page,
    Forgejo sets it to ``/api/v1/repos/{owner}/{repo}/pulls/{n}``.

    ``__init__`` performs network calls, so build the provider with ``__new__``
    and exercise the pure helper only.
    """

    @staticmethod
    def _provider():
        return GiteaProvider.__new__(GiteaProvider)

    def test_parse_pr_url_accepts_web_and_api_forms(self):
        provider = self._provider()
        assert provider._parse_pr_url("https://gitea.example.com/owner/repo/pulls/1") == ("owner", "repo", 1)
        assert provider._parse_pr_url(
            "https://gitea.example.com/api/v1/repos/owner/repo/pulls/1") == ("owner", "repo", 1)

    def test_parse_issue_url_accepts_web_and_api_forms(self):
        provider = self._provider()
        assert provider._parse_issue_url("https://gitea.example.com/owner/repo/issues/5") == ("owner", "repo", 5)
        assert provider._parse_issue_url(
            "https://gitea.example.com/api/v1/repos/owner/repo/issues/5") == ("owner", "repo", 5)
