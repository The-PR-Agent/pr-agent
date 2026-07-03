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
