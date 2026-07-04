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


class TestRepoApiFormalReview:
    """Tests for the formal-review HTTP calls on ``RepoApi``.

    These exercise the exact request the provider makes to Gitea's reviews API,
    mocking ``call_api`` so no real server is hit.
    """

    @staticmethod
    def _repo_api():
        from pr_agent.git_providers.gitea_provider import RepoApi

        client = MagicMock()
        return RepoApi(client), client

    def test_create_review_sends_event_and_commit_id(self):
        """A formal review POST must carry the ``event`` field (so it is
        submitted, not left as a PENDING draft) plus body/commit_id/comments."""
        repo_api, client = self._repo_api()

        repo_api.create_review(
            'owner', 'repo', 123,
            event='APPROVED',
            body='looks good',
            commit_id='deadbeef',
            comments=[],
        )

        args, kwargs = client.call_api.call_args
        assert args[0] == '/repos/{owner}/{repo}/pulls/{pr_number}/reviews'
        assert args[1] == 'POST'
        assert kwargs['path_params'] == {'owner': 'owner', 'repo': 'repo', 'pr_number': 123}
        body = kwargs['body']
        assert body['event'] == 'APPROVED'
        assert body['body'] == 'looks good'
        assert body['commit_id'] == 'deadbeef'
        assert body['comments'] == []
        # Regression: the reviews endpoint returns a review, not a Repository —
        # the response_type must not be the mistyped 'Repository'.
        assert kwargs['response_type'] is None
        assert kwargs.get('auth_settings') == ['AuthorizationHeaderToken']

    def test_create_review_without_event_is_pending_draft(self):
        """Omitting ``event`` must NOT inject one, leaving an unsubmitted draft."""
        repo_api, client = self._repo_api()

        repo_api.create_review('owner', 'repo', 123)

        _, kwargs = client.call_api.call_args
        assert 'event' not in kwargs['body']
        # commit_id is only sent when provided
        assert 'commit_id' not in kwargs['body']
        assert kwargs['body']['comments'] == []

    def test_create_review_forwards_inline_comments(self):
        repo_api, client = self._repo_api()

        comments = [{'body': 'nit', 'path': 'a.py', 'new_position': 3, 'old_position': 0}]
        repo_api.create_review(
            'owner', 'repo', 7, event='COMMENT', body='', commit_id='c0ffee', comments=comments,
        )

        _, kwargs = client.call_api.call_args
        assert kwargs['body']['comments'] == comments
        assert kwargs['body']['event'] == 'COMMENT'

    def test_create_inline_comment_defaults_to_pending_and_fixed_response_type(self):
        """The pre-existing inline-comment path must still work and no longer use
        the mistyped ``response_type='Repository'``."""
        repo_api, client = self._repo_api()

        repo_api.create_inline_comment(
            'owner', 'repo', 5, body='review', commit_id='abc123', comments=[],
        )

        _, kwargs = client.call_api.call_args
        assert 'event' not in kwargs['body']  # draft by default (unchanged behavior)
        assert kwargs['response_type'] is None
        assert kwargs['body']['commit_id'] == 'abc123'

    def test_create_inline_comment_accepts_event(self):
        repo_api, client = self._repo_api()

        repo_api.create_inline_comment(
            'owner', 'repo', 5, body='review', commit_id='abc123', comments=[], event='REQUEST_CHANGES',
        )

        _, kwargs = client.call_api.call_args
        assert kwargs['body']['event'] == 'REQUEST_CHANGES'

    def test_submit_pending_review_posts_to_review_id(self):
        repo_api, client = self._repo_api()

        repo_api.submit_pending_review('owner', 'repo', 123, review_id=99, event='APPROVED', body='ok')

        args, kwargs = client.call_api.call_args
        assert args[0] == '/repos/{owner}/{repo}/pulls/{pr_number}/reviews/{review_id}'
        assert args[1] == 'POST'
        assert kwargs['path_params'] == {
            'owner': 'owner', 'repo': 'repo', 'pr_number': 123, 'review_id': 99,
        }
        assert kwargs['body'] == {'event': 'APPROVED', 'body': 'ok'}
        assert kwargs['response_type'] is None


class TestGiteaProviderSubmitReview:
    """Tests for ``GiteaProvider.submit_review`` / ``auto_approve``.

    Built via ``__new__`` to bypass __init__'s network calls; only the
    attributes the methods touch are wired up.
    """

    @staticmethod
    def _provider():
        from pr_agent.git_providers.gitea_provider import GiteaProvider

        provider = GiteaProvider.__new__(GiteaProvider)
        provider.logger = MagicMock()
        provider.owner = 'owner'
        provider.repo = 'repo'
        provider.pr_number = 42
        provider.enabled_pr = True
        provider.last_commit = MagicMock()
        provider.last_commit.sha = 'headsha'
        provider.repo_api = MagicMock()
        return provider

    def test_submit_review_passes_event_and_head_commit(self):
        provider = self._provider()

        assert provider.submit_review('COMMENT', body='please look') is True

        provider.repo_api.create_review.assert_called_once_with(
            owner='owner',
            repo='repo',
            pr_number=42,
            event='COMMENT',
            body='please look',
            commit_id='headsha',
        )

    def test_submit_review_returns_false_on_api_error(self):
        """A failed formal review must be swallowed (return False), never raised,
        so it cannot break the underlying /review comment."""
        provider = self._provider()
        provider.repo_api.create_review.side_effect = Exception('boom')

        assert provider.submit_review('APPROVED') is False
        provider.logger.error.assert_called_once()

    def test_submit_review_refuses_when_not_a_pr(self):
        provider = self._provider()
        provider.enabled_pr = False

        assert provider.submit_review('APPROVED') is False
        provider.repo_api.create_review.assert_not_called()

    def test_auto_approve_submits_approved_review(self):
        """Mirrors github_provider.auto_approve: it must actually submit an
        APPROVED formal review (not the base-class no-op that returns False)."""
        provider = self._provider()

        assert provider.auto_approve() is True

        _, kwargs = provider.repo_api.create_review.call_args
        assert kwargs['event'] == 'APPROVED'
        assert kwargs['pr_number'] == 42

    def test_auto_approve_returns_false_on_error(self):
        provider = self._provider()
        provider.repo_api.create_review.side_effect = Exception('nope')

        assert provider.auto_approve() is False


class TestGiteaProviderLabels:
    """Tests for get_repo_labels + name-based publish_labels.

    Upstream's publish_labels forwarded whatever it was given straight to
    Gitea's issue-label endpoint, but every pr-agent tool passes label *names*
    while Gitea expects numeric label *ids*. The provider must resolve names to
    ids via the repository's labels first.
    """

    @staticmethod
    def _provider():
        from pr_agent.git_providers.gitea_provider import GiteaProvider

        provider = GiteaProvider.__new__(GiteaProvider)
        provider.logger = MagicMock()
        provider.owner = 'owner'
        provider.repo = 'repo'
        provider.pr_number = 7
        provider.enabled_pr = True
        provider.repo_api = MagicMock()
        return provider

    def test_get_repo_labels_returns_repo_labels(self):
        provider = self._provider()
        provider.repo_api.get_repo_labels.return_value = [
            {'id': 1, 'name': 'Bug fix', 'color': 'ff0000'},
            {'id': 2, 'name': 'Enhancement', 'color': '00ff00'},
        ]

        labels = provider.get_repo_labels()

        provider.repo_api.get_repo_labels.assert_called_once_with(owner='owner', repo='repo')
        assert [l['name'] for l in labels] == ['Bug fix', 'Enhancement']

    def test_get_repo_labels_empty_on_failure(self):
        provider = self._provider()
        provider.repo_api.get_repo_labels.return_value = []
        assert provider.get_repo_labels() == []

    def test_publish_labels_resolves_names_to_ids(self):
        """publish_labels receives NAMES; it must resolve them to Gitea label
        ids before posting (the core of item (a))."""
        provider = self._provider()
        provider.repo_api.get_repo_labels.return_value = [
            {'id': 10, 'name': 'Bug fix'},
            {'id': 20, 'name': 'Enhancement'},
            {'id': 30, 'name': 'Documentation'},
        ]

        provider.publish_labels(['Enhancement', 'Bug fix'])

        _, kwargs = provider.repo_api.add_labels.call_args
        assert kwargs['issue_number'] == 7
        assert kwargs['labels'] == [20, 10]

    def test_publish_labels_skips_unknown_names(self):
        """A name with no matching repo label is skipped (Gitea can only attach
        labels that already exist in the repo)."""
        provider = self._provider()
        provider.repo_api.get_repo_labels.return_value = [{'id': 10, 'name': 'Bug fix'}]

        provider.publish_labels(['Bug fix', 'Nonexistent'])

        _, kwargs = provider.repo_api.add_labels.call_args
        assert kwargs['labels'] == [10]

    def test_publish_labels_no_call_when_none_resolve(self):
        provider = self._provider()
        provider.repo_api.get_repo_labels.return_value = [{'id': 10, 'name': 'Bug fix'}]

        provider.publish_labels(['Totally Unknown'])

        provider.repo_api.add_labels.assert_not_called()

    def test_publish_labels_no_call_on_empty_input(self):
        provider = self._provider()
        provider.publish_labels([])
        provider.repo_api.add_labels.assert_not_called()

    def test_repo_api_get_repo_labels_hits_labels_endpoint(self):
        from pr_agent.git_providers.gitea_provider import RepoApi

        client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.data = BytesIO(b'[{"id": 1, "name": "Bug fix"}]')
        client.call_api.return_value = mock_resp
        repo_api = RepoApi(client)

        labels = repo_api.get_repo_labels('owner', 'repo')

        args, kwargs = client.call_api.call_args
        assert args[0] == '/repos/owner/repo/labels'
        assert args[1] == 'GET'
        assert kwargs.get('auth_settings') == ['AuthorizationHeaderToken']
        assert labels == [{'id': 1, 'name': 'Bug fix'}]


class TestGiteaProviderRemoveReaction:
    """Tests for the remove_reaction signature fix.

    Gitea previously defined remove_reaction(self, comment_id) while the base
    class and callers pass (issue_comment_id, reaction_id) -> the old signature
    raised a TypeError at runtime. The signature must match the base contract
    and return a bool.
    """

    @staticmethod
    def _provider():
        from pr_agent.git_providers.gitea_provider import GiteaProvider

        provider = GiteaProvider.__new__(GiteaProvider)
        provider.logger = MagicMock()
        provider.owner = 'owner'
        provider.repo = 'repo'
        provider.repo_api = MagicMock()
        return provider

    def test_remove_reaction_accepts_two_args_and_returns_true(self):
        provider = self._provider()

        # The base/caller contract passes BOTH a comment id and a reaction id.
        result = provider.remove_reaction(123, 456)

        assert result is True
        _, kwargs = provider.repo_api.remove_reaction_comment.call_args
        assert kwargs['comment_id'] == 123

    def test_remove_reaction_returns_false_on_error(self):
        provider = self._provider()
        provider.repo_api.remove_reaction_comment.side_effect = Exception('boom')

        assert provider.remove_reaction(123, 456) is False

    def test_remove_reaction_matches_base_signature(self):
        """Regression guard: the arity must match the base GitProvider so a
        caller passing (issue_comment_id, reaction_id) never hits a TypeError."""
        import inspect

        from pr_agent.git_providers.git_provider import GitProvider
        from pr_agent.git_providers.gitea_provider import GiteaProvider

        base_params = list(inspect.signature(GitProvider.remove_reaction).parameters)
        gitea_params = list(inspect.signature(GiteaProvider.remove_reaction).parameters)
        assert base_params == gitea_params == ['self', 'issue_comment_id', 'reaction_id']

    def test_repo_api_remove_reaction_sends_content_body(self):
        from pr_agent.git_providers.gitea_provider import RepoApi

        client = MagicMock()
        repo_api = RepoApi(client)

        repo_api.remove_reaction_comment('owner', 'repo', 99)

        args, kwargs = client.call_api.call_args
        assert args[0] == '/repos/{owner}/{repo}/issues/comments/{id}/reactions'
        assert args[1] == 'DELETE'
        assert kwargs['body'] == {'content': 'eyes'}
        assert kwargs['path_params'] == {'owner': 'owner', 'repo': 'repo', 'id': 99}


class TestGiteaProviderIsSupported:
    """is_supported must reflect real capability, not unconditionally True."""

    @staticmethod
    def _provider():
        from pr_agent.git_providers.gitea_provider import GiteaProvider

        provider = GiteaProvider.__new__(GiteaProvider)
        provider.logger = MagicMock()
        return provider

    @patch('pr_agent.git_providers.gitea_provider.get_settings')
    def test_push_code_unsupported_in_restricted_mode(self, mock_get_settings):
        settings = MagicMock()
        settings.config.restricted_mode = True
        mock_get_settings.return_value = settings

        provider = self._provider()
        assert provider.is_supported("push_code") is False

    @patch('pr_agent.git_providers.gitea_provider.get_settings')
    def test_push_code_supported_when_not_restricted(self, mock_get_settings):
        settings = MagicMock()
        settings.config.restricted_mode = False
        mock_get_settings.return_value = settings

        provider = self._provider()
        assert provider.is_supported("push_code") is True

    @patch('pr_agent.git_providers.gitea_provider.get_settings')
    def test_other_capabilities_supported(self, mock_get_settings):
        settings = MagicMock()
        settings.config.restricted_mode = True
        mock_get_settings.return_value = settings

        provider = self._provider()
        # A non-push capability stays supported even in restricted mode.
        assert provider.is_supported("get_labels") is True


class TestGiteaProviderCommentById:
    """Tests for the comment-by-id methods that unlock inline /ask.

    These were base-class no-ops on Gitea; they are implemented against Gitea's
    issue/PR comment REST endpoints.
    """

    @staticmethod
    def _provider():
        from pr_agent.git_providers.gitea_provider import GiteaProvider

        provider = GiteaProvider.__new__(GiteaProvider)
        provider.logger = MagicMock()
        provider.owner = 'owner'
        provider.repo = 'repo'
        provider.pr_number = 5
        provider.enabled_pr = True
        provider.enabled_issue = False
        provider.max_comment_chars = 65000
        provider.repo_api = MagicMock()
        return provider

    def test_edit_comment_from_comment_id_calls_edit(self):
        provider = self._provider()

        provider.edit_comment_from_comment_id(321, 'updated body')

        _, kwargs = provider.repo_api.edit_comment.call_args
        assert kwargs['comment_id'] == 321
        assert kwargs['comment'] == 'updated body'

    def test_get_comment_body_from_comment_id_returns_body(self):
        provider = self._provider()
        provider.repo_api.get_comment.return_value = {'id': 321, 'body': 'hello'}

        assert provider.get_comment_body_from_comment_id(321) == 'hello'
        _, kwargs = provider.repo_api.get_comment.call_args
        assert kwargs['comment_id'] == 321

    def test_get_comment_body_from_comment_id_empty_on_missing(self):
        provider = self._provider()
        provider.repo_api.get_comment.return_value = {}
        assert provider.get_comment_body_from_comment_id(321) == ''

    def test_reply_to_comment_from_comment_id_posts_comment(self):
        provider = self._provider()
        provider.publish_comment = MagicMock()

        provider.reply_to_comment_from_comment_id(321, 'my answer')

        provider.publish_comment.assert_called_once_with('my answer')

    def test_get_review_thread_comments_returns_target_comment(self):
        provider = self._provider()
        provider.repo_api.get_comment.return_value = {'id': 321, 'body': 'original'}

        thread = provider.get_review_thread_comments(321)

        assert thread == [{'id': 321, 'body': 'original'}]

    def test_get_review_thread_comments_empty_when_missing(self):
        provider = self._provider()
        provider.repo_api.get_comment.return_value = {}
        assert provider.get_review_thread_comments(321) == []

    def test_repo_api_get_comment_hits_issue_comment_endpoint(self):
        from pr_agent.git_providers.gitea_provider import RepoApi

        client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.data = BytesIO(b'{"id": 321, "body": "hi"}')
        client.call_api.return_value = mock_resp
        repo_api = RepoApi(client)

        comment = repo_api.get_comment('owner', 'repo', 321)

        args, kwargs = client.call_api.call_args
        assert args[0] == '/repos/owner/repo/issues/comments/321'
        assert args[1] == 'GET'
        assert kwargs.get('auth_settings') == ['AuthorizationHeaderToken']
        assert comment == {'id': 321, 'body': 'hi'}


class TestGiteaProviderConfigBranch:
    """get_repo_settings config-branch support (mirrors GithubProvider)."""

    @staticmethod
    def _provider():
        from pr_agent.git_providers.gitea_provider import GiteaProvider

        provider = GiteaProvider.__new__(GiteaProvider)
        provider.logger = MagicMock()
        provider.owner = 'owner'
        provider.repo = 'repo'
        provider.sha = 'headsha'
        provider.repo_settings = '.pr_agent.toml'
        provider.repo_api = MagicMock()
        return provider

    @patch.dict('os.environ', {}, clear=True)
    @patch('pr_agent.git_providers.gitea_provider.get_settings')
    def test_reads_from_config_branch_when_set(self, mock_get_settings):
        settings = MagicMock()
        settings.get.side_effect = lambda k, d=None: {'CONFIG.CONFIG_BRANCH': 'config-branch'}.get(k, d)
        mock_get_settings.return_value = settings

        provider = self._provider()
        provider.repo_api.get_file_content.return_value = '[config]\nx = 1\n'

        result = provider.get_repo_settings()

        # The ref passed to Gitea must be the config branch, not the head sha.
        _, kwargs = provider.repo_api.get_file_content.call_args
        assert kwargs['commit_sha'] == 'config-branch'
        assert result == b'[config]\nx = 1\n'

    @patch.dict('os.environ', {'PR_AGENT_CONFIG_BRANCH': 'env-branch'}, clear=True)
    @patch('pr_agent.git_providers.gitea_provider.get_settings')
    def test_reads_from_env_branch_when_setting_unset(self, mock_get_settings):
        settings = MagicMock()
        settings.get.side_effect = lambda k, d=None: {}.get(k, d)
        mock_get_settings.return_value = settings

        provider = self._provider()
        provider.repo_api.get_file_content.return_value = 'data'

        provider.get_repo_settings()

        _, kwargs = provider.repo_api.get_file_content.call_args
        assert kwargs['commit_sha'] == 'env-branch'

    @patch.dict('os.environ', {}, clear=True)
    @patch('pr_agent.git_providers.gitea_provider.get_settings')
    def test_falls_back_to_head_sha_when_branch_missing_file(self, mock_get_settings):
        settings = MagicMock()
        settings.get.side_effect = lambda k, d=None: {'CONFIG.CONFIG_BRANCH': 'config-branch'}.get(k, d)
        mock_get_settings.return_value = settings

        provider = self._provider()
        # First call (config branch) misses -> '', second (head sha) succeeds.
        provider.repo_api.get_file_content.side_effect = ['', 'from-head']

        result = provider.get_repo_settings()

        assert result == b'from-head'
        assert provider.repo_api.get_file_content.call_count == 2
        second_call_kwargs = provider.repo_api.get_file_content.call_args_list[1].kwargs
        assert second_call_kwargs['commit_sha'] == 'headsha'

    @patch.dict('os.environ', {}, clear=True)
    @patch('pr_agent.git_providers.gitea_provider.get_settings')
    def test_reads_from_head_sha_when_no_config_branch(self, mock_get_settings):
        settings = MagicMock()
        settings.get.side_effect = lambda k, d=None: {}.get(k, d)
        mock_get_settings.return_value = settings

        provider = self._provider()
        provider.repo_api.get_file_content.return_value = 'toml'

        provider.get_repo_settings()

        # No config branch -> a single read from the head sha.
        provider.repo_api.get_file_content.assert_called_once()
        _, kwargs = provider.repo_api.get_file_content.call_args
        assert kwargs['commit_sha'] == 'headsha'
