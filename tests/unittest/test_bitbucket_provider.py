from unittest.mock import MagicMock, patch

import pytest
from atlassian.bitbucket import Bitbucket
from requests.exceptions import HTTPError

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.git_providers import BitbucketServerProvider
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider


class TestBitbucketProvider:
    def test_parse_pr_url(self):
        url = "https://bitbucket.org/WORKSPACE_XYZ/MY_TEST_REPO/pull-requests/321"
        workspace_slug, repo_slug, pr_number = BitbucketProvider._parse_pr_url(url)
        assert workspace_slug == "WORKSPACE_XYZ"
        assert repo_slug == "MY_TEST_REPO"
        assert pr_number == 321

    def test_get_repo_file_content_reads_from_target_branch(self):
        # Repo-context files must be read from the PR destination (target) branch,
        # matching the other providers.
        provider = BitbucketProvider.__new__(BitbucketProvider)
        provider.pr = MagicMock(destination_branch="release-1.0")
        provider.get_pr_file_content = MagicMock(return_value="repo context")

        content = provider.get_repo_file_content("AGENTS.md")

        assert content == "repo context"
        provider.get_pr_file_content.assert_called_once_with("AGENTS.md", "release-1.0")

    def test_get_repo_file_content_from_default_branch(self):
        provider = BitbucketProvider.__new__(BitbucketProvider)
        provider.pr = MagicMock(destination_branch="release-1.0")
        provider.get_repo_default_branch = MagicMock(return_value="main")
        provider.get_pr_file_content = MagicMock(return_value="repo context")

        content = provider.get_repo_file_content("AGENTS.md", from_default_branch=True)

        assert content == "repo context"
        provider.get_pr_file_content.assert_called_once_with("AGENTS.md", "main")


class TestBitbucketServerProvider:
    def test_parse_pr_url(self):
        url = "https://git.onpreminstance.com/projects/AAA/repos/my-repo/pull-requests/1"
        workspace_slug, repo_slug, pr_number = BitbucketServerProvider._parse_pr_url(url)
        assert workspace_slug == "AAA"
        assert repo_slug == "my-repo"
        assert pr_number == 1

    def test_parse_pr_url_with_users(self):
        url = "https://bitbucket.company-server.url/users/username/repos/my-repo/pull-requests/1"
        workspace_slug, repo_slug, pr_number = BitbucketServerProvider._parse_pr_url(url)
        assert workspace_slug == "~username"
        assert repo_slug == "my-repo"
        assert pr_number == 1

    def test_get_repo_file_content_reads_from_target_ref(self):
        # Repo-context files must be read from the PR target ref (toRef), matching
        # the other providers.
        provider = BitbucketServerProvider.__new__(BitbucketServerProvider)
        provider.pr = MagicMock(toRef={"latestCommit": "base-sha"})
        provider.get_file = MagicMock(return_value="repo context")

        content = provider.get_repo_file_content("AGENTS.md")

        assert content == "repo context"
        provider.get_file.assert_called_once_with("AGENTS.md", "base-sha")

    def test_get_repo_file_content_from_default_branch(self):
        provider = BitbucketServerProvider.__new__(BitbucketServerProvider)
        provider.workspace_slug = "AAA"
        provider.repo_slug = "my-repo"
        provider.pr = MagicMock(toRef={"latestCommit": "base-sha"})
        provider.bitbucket_client = MagicMock()
        provider.bitbucket_client.get_default_branch.return_value = {"displayId": "main"}
        provider.get_file = MagicMock(return_value="repo context")

        content = provider.get_repo_file_content("AGENTS.md", from_default_branch=True)

        assert content == "repo context"
        provider.get_file.assert_called_once_with("AGENTS.md", "main")

    def _make_provider_for_repo_settings(self, get_content_side_effect):
        # Bypass __init__ (which performs live API calls) and only wire up the
        # attributes get_repo_settings() relies on.
        provider = BitbucketServerProvider.__new__(BitbucketServerProvider)
        provider.workspace_slug = "AAA"
        provider.repo_slug = "my-repo"
        provider.bitbucket_client = MagicMock(Bitbucket)
        provider.bitbucket_client.get_content_of_file.side_effect = get_content_side_effect
        return provider

    def test_get_repo_settings_missing_file_not_logged_as_error(self):
        # A missing .pr_agent.toml is expected/optional and must not be logged as an
        # error, matching the other git providers (issue #2481).
        def raise_not_found(*args, **kwargs):
            raise Exception("File not found")

        provider = self._make_provider_for_repo_settings(raise_not_found)

        with patch("pr_agent.git_providers.bitbucket_server_provider.get_logger") as mock_get_logger:
            logger = mock_get_logger.return_value
            result = provider.get_repo_settings()

        assert result == ""
        logger.error.assert_not_called()
        logger.info.assert_called_once()

    def test_get_repo_settings_404_returns_empty_silently(self):
        response = MagicMock()
        response.status_code = 404
        http_error = HTTPError("404 Not Found")
        http_error.response = response

        def raise_http_404(*args, **kwargs):
            raise http_error

        provider = self._make_provider_for_repo_settings(raise_http_404)

        with patch("pr_agent.git_providers.bitbucket_server_provider.get_logger") as mock_get_logger:
            logger = mock_get_logger.return_value
            result = provider.get_repo_settings()

        assert result == ""
        logger.error.assert_not_called()
        logger.info.assert_not_called()

    def mock_get_content_of_file(self, project_key, repository_slug, filename, at=None, markup=None):
        content_map = {
            '9c1cffdd9f276074bfb6fb3b70fbee62d298b058': 'file\nwith\nsome\nlines\nto\nemulate\na\nreal\nfile\n',
            '2a1165446bdf991caf114d01f7c88d84ae7399cf': 'file\nwith\nmultiple \nlines\nto\nemulate\na\nfake\nfile\n',
            'f617708826cdd0b40abb5245eda71630192a17e3': 'file\nwith\nmultiple \nlines\nto\nemulate\na\nreal\nfile\n',
            'cb68a3027d6dda065a7692ebf2c90bed1bcdec28': 'file\nwith\nsome\nchanges\nto\nemulate\na\nreal\nfile\n',
            '1905dcf16c0aac6ac24f7ab617ad09c73dc1d23b': 'file\nwith\nsome\nlines\nto\nemulate\na\nfake\ntest\n',
            'ae4eca7f222c96d396927d48ab7538e2ee13ca63': 'readme\nwithout\nsome\nlines\nto\nsimulate\na\nreal\nfile',
            '548f8ba15abc30875a082156314426806c3f4d97': 'file\nwith\nsome\nlines\nto\nemulate\na\nreal\nfile',
            '0e898cb355a5170d8c8771b25d43fcaa1d2d9489': 'file\nwith\nmultiple\nlines\nto\nemulate\na\nreal\nfile'
        }
        return content_map.get(at, '')

    def mock_get_from_bitbucket_60(self, url):
        response_map = {
            "rest/api/1.0/application-properties": {
                "version": "6.0"
            }
        }
        return response_map.get(url, '')

    def mock_get_from_bitbucket_70(self, url):
        response_map = {
            "rest/api/1.0/application-properties": {
                "version": "7.0"
            }
        }
        return response_map.get(url, '')

    def mock_get_from_bitbucket_816(self, url):
        response_map = {
            "rest/api/1.0/application-properties": {
                "version": "8.16"
            },
            "rest/api/latest/projects/AAA/repos/my-repo/pull-requests/1/merge-base": {
                'id': '548f8ba15abc30875a082156314426806c3f4d97'
            }
        }
        return response_map.get(url, '')


    '''
    tests the 2-way diff functionality where the diff should be between the HEAD of branch b and node c
    NOT between the HEAD of main and the HEAD of branch b

          - o  branch b
         /
    o - o - o  main
        ^ node c
    '''
    def test_get_diff_files_simple_diverge_70(self):
        bitbucket_client = MagicMock(Bitbucket)
        bitbucket_client.get_pull_request.return_value = {
            'toRef': {'latestCommit': '9c1cffdd9f276074bfb6fb3b70fbee62d298b058'},
            'fromRef': {'latestCommit': '2a1165446bdf991caf114d01f7c88d84ae7399cf'}
        }
        bitbucket_client.get_pull_requests_commits.return_value = [
            {'id': '2a1165446bdf991caf114d01f7c88d84ae7399cf',
             'parents': [{'id': 'f617708826cdd0b40abb5245eda71630192a17e3'}]}
        ]
        bitbucket_client.get_commits.return_value = [
            {'id': '9c1cffdd9f276074bfb6fb3b70fbee62d298b058'},
            {'id': 'dbca09554567d2e4bee7f07993390153280ee450'}
        ]
        bitbucket_client.get_pull_requests_changes.return_value = [
            {
                'path': {'toString': 'Readme.md'},
                'type': 'MODIFY',
            }
        ]

        bitbucket_client.get.side_effect = self.mock_get_from_bitbucket_70
        bitbucket_client.get_content_of_file.side_effect = self.mock_get_content_of_file

        provider = BitbucketServerProvider(
            "https://git.onpreminstance.com/projects/AAA/repos/my-repo/pull-requests/1",
            bitbucket_client=bitbucket_client
        )

        expected = [
            FilePatchInfo(
                'file\nwith\nmultiple \nlines\nto\nemulate\na\nreal\nfile\n',
                'file\nwith\nmultiple \nlines\nto\nemulate\na\nfake\nfile\n',
                '--- \n+++ \n@@ -5,5 +5,5 @@\n to\n emulate\n a\n-real\n+fake\n file\n',
                'Readme.md',
                edit_type=EDIT_TYPE.MODIFIED,
            )
        ]

        actual = provider.get_diff_files()

        assert actual == expected


    '''
    tests the 2-way diff functionality where the diff should be between the HEAD of branch b and node c
    NOT between the HEAD of main and the HEAD of branch b

          - o - o - o  branch b
         /     /
    o - o -- o - o     main
             ^ node c
    '''
    def test_get_diff_files_diverge_with_merge_commit_70(self):
        bitbucket_client = MagicMock(Bitbucket)
        bitbucket_client.get_pull_request.return_value = {
            'toRef': {'latestCommit': 'cb68a3027d6dda065a7692ebf2c90bed1bcdec28'},
            'fromRef': {'latestCommit': '1905dcf16c0aac6ac24f7ab617ad09c73dc1d23b'}
        }
        bitbucket_client.get_pull_requests_commits.return_value = [
            {'id': '1905dcf16c0aac6ac24f7ab617ad09c73dc1d23b',
             'parents': [{'id': '692772f456c3db77a90b11ce39ea516f8c2bad93'}]},
            {'id': '692772f456c3db77a90b11ce39ea516f8c2bad93', 'parents': [
                {'id': '2a1165446bdf991caf114d01f7c88d84ae7399cf'},
                {'id': '9c1cffdd9f276074bfb6fb3b70fbee62d298b058'},
            ]},
            {'id': '2a1165446bdf991caf114d01f7c88d84ae7399cf',
             'parents': [{'id': 'f617708826cdd0b40abb5245eda71630192a17e3'}]}
        ]
        bitbucket_client.get_commits.return_value = [
            {'id': 'cb68a3027d6dda065a7692ebf2c90bed1bcdec28'},
            {'id': '9c1cffdd9f276074bfb6fb3b70fbee62d298b058'},
            {'id': 'dbca09554567d2e4bee7f07993390153280ee450'}
        ]
        bitbucket_client.get_pull_requests_changes.return_value = [
            {
                'path': {'toString': 'Readme.md'},
                'type': 'MODIFY',
            }
        ]

        bitbucket_client.get.side_effect = self.mock_get_from_bitbucket_70
        bitbucket_client.get_content_of_file.side_effect = self.mock_get_content_of_file

        provider = BitbucketServerProvider(
            "https://git.onpreminstance.com/projects/AAA/repos/my-repo/pull-requests/1",
            bitbucket_client=bitbucket_client
        )

        expected = [
            FilePatchInfo(
                'file\nwith\nsome\nlines\nto\nemulate\na\nreal\nfile\n',
                'file\nwith\nsome\nlines\nto\nemulate\na\nfake\ntest\n',
                '--- \n+++ \n@@ -5,5 +5,5 @@\n to\n emulate\n a\n-real\n-file\n+fake\n+test\n',
                'Readme.md',
                edit_type=EDIT_TYPE.MODIFIED,
            )
        ]

        actual = provider.get_diff_files()

        assert actual == expected


    '''
    tests the 2-way diff functionality where the diff should be between the HEAD of branch c and node d
    NOT between the HEAD of main and the HEAD of branch c

            ---- o - o branch c
           /    /
          ---- o       branch b
         /    /
        o - o - o      main
            ^ node d
    '''
    def get_multi_merge_diverge_mock_client(self, api_version):
        bitbucket_client = MagicMock(Bitbucket)
        bitbucket_client.get_pull_request.return_value = {
            'toRef': {'latestCommit': '9569922b22fe4fd0968be6a50ed99f71efcd0504'},
            'fromRef': {'latestCommit': 'ae4eca7f222c96d396927d48ab7538e2ee13ca63'}
        }
        bitbucket_client.get_pull_requests_commits.return_value = [
            {'id': 'ae4eca7f222c96d396927d48ab7538e2ee13ca63',
             'parents': [{'id': 'bbf300fb3af5129af8c44659f8cc7a526a6a6f31'}]},
            {'id': 'bbf300fb3af5129af8c44659f8cc7a526a6a6f31', 'parents': [
                {'id': '10b7b8e41cb370b48ceda8da4e7e6ad033182213'},
                {'id': 'd1bb183c706a3ebe4c2b1158c25878201a27ad8c'},
            ]},
            {'id': 'd1bb183c706a3ebe4c2b1158c25878201a27ad8c', 'parents': [
                {'id': '5bd76251866cb415fc5ff232f63a581e89223bda'},
                {'id': '548f8ba15abc30875a082156314426806c3f4d97'}
            ]},
            {'id': '5bd76251866cb415fc5ff232f63a581e89223bda',
             'parents': [{'id': '0e898cb355a5170d8c8771b25d43fcaa1d2d9489'}]},
            {'id': '10b7b8e41cb370b48ceda8da4e7e6ad033182213',
             'parents': [{'id': '0e898cb355a5170d8c8771b25d43fcaa1d2d9489'}]}
        ]
        bitbucket_client.get_commits.return_value = [
            {'id': '9569922b22fe4fd0968be6a50ed99f71efcd0504'},
            {'id': '548f8ba15abc30875a082156314426806c3f4d97'}
        ]
        bitbucket_client.get_pull_requests_changes.return_value = [
            {
                'path': {'toString': 'Readme.md'},
                'type': 'MODIFY',
            }
        ]

        bitbucket_client.get_content_of_file.side_effect = self.mock_get_content_of_file
        if api_version == 60:
            bitbucket_client.get.side_effect = self.mock_get_from_bitbucket_60
        elif api_version == 70:
            bitbucket_client.get.side_effect = self.mock_get_from_bitbucket_70
        elif api_version == 816:
            bitbucket_client.get.side_effect = self.mock_get_from_bitbucket_816

        return bitbucket_client

    def test_get_diff_files_multi_merge_diverge_60(self):
        bitbucket_client = self.get_multi_merge_diverge_mock_client(60)

        provider = BitbucketServerProvider(
            "https://git.onpreminstance.com/projects/AAA/repos/my-repo/pull-requests/1",
            bitbucket_client=bitbucket_client
        )

        expected = [
            FilePatchInfo(
                'file\nwith\nmultiple\nlines\nto\nemulate\na\nreal\nfile',
                'readme\nwithout\nsome\nlines\nto\nsimulate\na\nreal\nfile',
                '--- \n+++ \n@@ -1,9 +1,9 @@\n-file\n-with\n-multiple\n+readme\n+without\n+some\n lines\n to\n-emulate\n+simulate\n a\n real\n file\n',
                'Readme.md',
                edit_type=EDIT_TYPE.MODIFIED,
            )
        ]

        actual = provider.get_diff_files()

        assert actual == expected

    def test_get_diff_files_multi_merge_diverge_70(self):
        bitbucket_client = self.get_multi_merge_diverge_mock_client(70)

        provider = BitbucketServerProvider(
            "https://git.onpreminstance.com/projects/AAA/repos/my-repo/pull-requests/1",
            bitbucket_client=bitbucket_client
        )

        expected = [
            FilePatchInfo(
                'file\nwith\nsome\nlines\nto\nemulate\na\nreal\nfile',
                'readme\nwithout\nsome\nlines\nto\nsimulate\na\nreal\nfile',
                '--- \n+++ \n@@ -1,9 +1,9 @@\n-file\n-with\n+readme\n+without\n some\n lines\n to\n-emulate\n+simulate\n a\n real\n file\n',
                'Readme.md',
                edit_type=EDIT_TYPE.MODIFIED,
            )
        ]

        actual = provider.get_diff_files()

        assert actual == expected

    def test_get_diff_files_multi_merge_diverge_816(self):
        bitbucket_client = self.get_multi_merge_diverge_mock_client(816)

        provider = BitbucketServerProvider(
            "https://git.onpreminstance.com/projects/AAA/repos/my-repo/pull-requests/1",
            bitbucket_client=bitbucket_client
        )

        expected = [
            FilePatchInfo(
                'file\nwith\nsome\nlines\nto\nemulate\na\nreal\nfile',
                'readme\nwithout\nsome\nlines\nto\nsimulate\na\nreal\nfile',
                '--- \n+++ \n@@ -1,9 +1,9 @@\n-file\n-with\n+readme\n+without\n some\n lines\n to\n-emulate\n+simulate\n a\n real\n file\n',
                'Readme.md',
                edit_type=EDIT_TYPE.MODIFIED,
            )
        ]

        actual = provider.get_diff_files()

        assert actual == expected


@pytest.fixture(autouse=True)
def _clear_global_settings_cache():
    from pr_agent.git_providers import git_provider as _gp
    _gp._GLOBAL_SETTINGS_CACHE.clear()
    yield
    _gp._GLOBAL_SETTINGS_CACHE.clear()


class TestBitbucketGlobalSettings:
    def _provider(self):
        provider = BitbucketProvider.__new__(BitbucketProvider)
        provider.workspace_slug = "myws"
        provider.headers = {"Authorization": "Bearer x"}
        return provider

    def test_loads_workspace_pr_agent_settings(self):
        provider = self._provider()
        repo_resp = MagicMock(status_code=200)
        repo_resp.json.return_value = {"mainbranch": {"name": "main"}}
        file_resp = MagicMock(status_code=200)
        file_resp.text = "[pr_reviewer]\nnum_max_findings = 5\n"
        with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
                   side_effect=[repo_resp, file_resp]) as rq, \
             patch("pr_agent.git_providers.bitbucket_provider.get_settings") as ms:
            ms.return_value.config.use_global_settings_file = True
            result = provider._get_global_repo_settings()
        assert result == b"[pr_reviewer]\nnum_max_findings = 5\n"
        assert rq.call_count == 2  # repo info + file
        assert "myws/pr-agent-settings" in rq.call_args_list[0].args[1]
        assert "src/main/.pr_agent.toml" in rq.call_args_list[1].args[1]

    def test_no_access_403_returns_empty_and_caches(self):
        # A 403 (no access) is a stable/expected condition like 404: return "" AND cache it.
        provider = self._provider()
        repo_resp = MagicMock(status_code=403)
        with patch("pr_agent.git_providers.bitbucket_provider.requests.request", return_value=repo_resp) as rq, \
             patch("pr_agent.git_providers.bitbucket_provider.get_settings") as ms:
            ms.return_value.config.use_global_settings_file = True
            assert provider._get_global_repo_settings() == ""
            assert provider._get_global_repo_settings() == ""  # served from cache
        assert rq.call_count == 1

    def test_missing_settings_repo_returns_empty(self):
        provider = self._provider()
        repo_resp = MagicMock(status_code=404)
        with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
                   return_value=repo_resp), \
             patch("pr_agent.git_providers.bitbucket_provider.get_settings") as ms:
            ms.return_value.config.use_global_settings_file = True
            assert provider._get_global_repo_settings() == ""

    def test_disabled_returns_empty(self):
        provider = self._provider()
        with patch("pr_agent.git_providers.bitbucket_provider.requests.request") as rq, \
             patch("pr_agent.git_providers.bitbucket_provider.get_settings") as ms:
            ms.return_value.config.use_global_settings_file = False
            assert provider._get_global_repo_settings() == ""
        rq.assert_not_called()

    def test_result_is_cached(self):
        provider = self._provider()
        repo_resp = MagicMock(status_code=200)
        repo_resp.json.return_value = {"mainbranch": {"name": "main"}}
        file_resp = MagicMock(status_code=200)
        file_resp.text = "[pr_reviewer]\nx = 1\n"
        with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
                   side_effect=[repo_resp, file_resp]) as rq, \
             patch("pr_agent.git_providers.bitbucket_provider.get_settings") as ms:
            ms.return_value.config.use_global_settings_file = True
            provider._get_global_repo_settings()
            provider._get_global_repo_settings()
        # Two HTTP calls total (first fetch), none on the cached second call.
        assert rq.call_count == 2


class TestBitbucketLocalSettingsRobustness:
    def test_get_repo_settings_ignores_error_response_for_local(self):
        # A non-200/404 response (e.g. 500 error page) must NOT be treated as local TOML content.
        provider = BitbucketProvider.__new__(BitbucketProvider)
        provider.workspace_slug = "myws"
        provider.repo_slug = "myrepo"
        provider.headers = {"Authorization": "Bearer x"}
        provider.pr = MagicMock(destination_branch="main")
        resp = MagicMock(status_code=500)
        resp.text = "<html>internal error</html>"
        with patch("pr_agent.git_providers.bitbucket_provider.requests.request", return_value=resp), \
             patch("pr_agent.git_providers.bitbucket_provider.get_settings") as ms:
            ms.return_value.config.use_global_settings_file = False
            result = provider.get_repo_settings()
        assert result == ""
