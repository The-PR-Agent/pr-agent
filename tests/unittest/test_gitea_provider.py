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
