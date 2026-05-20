from io import BytesIO
from unittest.mock import MagicMock, patch

from pr_agent.git_providers.gitea_provider import GiteaProvider


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

    def test_get_repo_file_content_falls_back_to_head_sha_when_base_missing(self):
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

        assert content == "repo context"
        provider.repo_api.get_file_content.assert_called_once_with(
            owner="owner",
            repo="repo",
            commit_sha="head-sha",
            filepath="AGENTS.md"
        )
