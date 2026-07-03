from io import BytesIO
from unittest.mock import MagicMock, patch


class TestGiteaProvider:
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
