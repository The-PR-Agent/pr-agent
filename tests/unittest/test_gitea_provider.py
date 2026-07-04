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

    def test_submit_review_passes_event_and_omits_commit_id(self):
        """``submit_review`` must NOT pin ``commit_id``: ``self.last_commit`` is
        the repo/default-branch head (from ``repo_get_all_commits``), not the PR
        head, so pinning it anchors the review to the wrong sha. Omitting it lets
        Gitea default to the PR head."""
        provider = self._provider()

        assert provider.submit_review('COMMENT', body='please look') is True

        provider.repo_api.create_review.assert_called_once_with(
            owner='owner',
            repo='repo',
            pr_number=42,
            event='COMMENT',
            body='please look',
        )
        _, kwargs = provider.repo_api.create_review.call_args
        assert 'commit_id' not in kwargs

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


class TestGiteaProviderPublishInlineComments:
    """``publish_inline_comments`` submits a COMMENT review (not a PENDING draft).

    It runs ``create_review`` with ``_preload_content=False``, so a successful
    call returns the ``(data, status, headers)`` tuple, not a truthy body. The
    success/failure log must key off the HTTP status, not the (always-falsy)
    body. The review must carry ``event="COMMENT"`` so the inline comments post
    immediately instead of lingering as an unsubmitted draft, and must NOT pin a
    (wrong) ``commit_id`` so Gitea anchors positions to the PR head."""

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

    def test_logs_success_on_2xx_tuple(self):
        provider = self._provider()
        # giteapy returns (data, status, headers); data is None with _preload_content=False
        provider.repo_api.create_review.return_value = (None, 201, {})

        provider.publish_inline_comments([{'body': 'x'}])

        provider.logger.info.assert_called_once()
        provider.logger.error.assert_not_called()

    def test_logs_failure_on_non_2xx_tuple(self):
        provider = self._provider()
        provider.repo_api.create_review.return_value = (None, 422, {})

        provider.publish_inline_comments([{'body': 'x'}])

        provider.logger.error.assert_called_once()
        provider.logger.info.assert_not_called()

    def test_submits_as_comment_event_with_the_comments(self):
        """The inline comments must be submitted as a COMMENT review, carrying
        the exact comment payloads, so they post immediately (not as a draft)."""
        provider = self._provider()
        provider.repo_api.create_review.return_value = (None, 201, {})

        comments = [{'body': 'nit', 'path': 'a.py', 'new_position': 3, 'old_position': 0}]
        provider.publish_inline_comments(comments, body='Suggestion body')

        _, kwargs = provider.repo_api.create_review.call_args
        assert kwargs['event'] == 'COMMENT'
        assert kwargs['body'] == 'Suggestion body'
        assert kwargs['comments'] == comments
        assert kwargs['pr_number'] == 42
        # commit_id must NOT be pinned (self.last_commit is the wrong sha).
        assert 'commit_id' not in kwargs
        # The old PENDING-draft path must no longer be used.
        provider.repo_api.create_inline_comment.assert_not_called()

    def test_inline_comments_do_not_submit_an_approval(self):
        """Guard: inline comments must never smuggle an APPROVED/REQUEST_CHANGES
        event — the formal verdict is a separate submit_review call."""
        provider = self._provider()
        provider.repo_api.create_review.return_value = (None, 201, {})

        provider.publish_inline_comments([{'body': 'x'}])

        _, kwargs = provider.repo_api.create_review.call_args
        assert kwargs['event'] == 'COMMENT'
        assert kwargs['event'] not in ('APPROVED', 'REQUEST_CHANGES')


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


class TestGiteaProviderGetDiffFiles:
    """Tests for the hardened ``get_diff_files`` — the core review input.

    The previous implementation pulled the patch straight from the hand-parsed
    PR ``.diff`` and dropped the patch for files with no ``@@`` hunk (pure
    renames, mode changes, binaries), and read base content from the raw
    ``base.sha`` instead of the merge base. The hardened version:
      * reads base content from the PR merge base (``merge_base_sha``),
      * prefers the merge-base-relative compare diff for patches, falling back
        to the PR ``.diff`` when the compare API is unavailable,
      * synthesizes a patch via ``load_large_diff`` for any changed file the
        parser could not attribute, so renames/binaries are never dropped.

    Built via ``__new__`` to bypass __init__'s network calls; only the
    attributes ``get_diff_files`` touches are wired up.
    """

    @staticmethod
    def _provider(git_files, file_diffs=None, file_contents=None,
                  base_sha='basesha', head_sha='headsha', merge_base_sha='mbsha',
                  compare_diff=None):
        from pr_agent.git_providers.gitea_provider import GiteaProvider

        provider = GiteaProvider.__new__(GiteaProvider)
        provider.logger = MagicMock()
        provider.owner = 'owner'
        provider.repo = 'repo'
        provider.pr_number = 1
        provider.enabled_pr = True
        provider.git_files = git_files
        provider.file_diffs = file_diffs or {}
        provider.file_contents = file_contents or {}
        provider.sha = head_sha
        provider.base_sha = base_sha
        provider.merge_base_sha = merge_base_sha
        provider.diff_files = []
        provider.incremental = __import__(
            'pr_agent.git_providers.git_provider', fromlist=['IncrementalPR']
        ).IncrementalPR(False)
        provider.unreviewed_files_set = {}
        provider.repo_api = MagicMock()
        # base content read: return "" unless overridden by the test
        provider.repo_api.get_file_content.return_value = ""
        # compare diff: default to none so the PR .diff (file_diffs) is used
        provider.repo_api.get_compare_diff.return_value = compare_diff or ""
        return provider

    def _patch_valid_file(self):
        # is_valid_file consults settings; patch it to accept every .py/.txt path
        return patch('pr_agent.git_providers.gitea_provider.is_valid_file', return_value=True)

    def test_multi_hunk_patch_is_carried_through(self):
        from pr_agent.algo.types import EDIT_TYPE
        git_files = [{'filename': 'a.py', 'status': 'modified', 'additions': 2, 'deletions': 0}]
        multi_hunk = (
            '@@ -1,2 +1,3 @@\n a\n+b\n c\n'
            '@@ -20,2 +21,3 @@\n d\n+e\n f'
        )
        provider = self._provider(git_files, file_diffs={'a.py': multi_hunk})
        with self._patch_valid_file():
            diff_files = provider.get_diff_files()

        assert len(diff_files) == 1
        f = diff_files[0]
        assert f.filename == 'a.py'
        assert f.patch.count('@@ -') == 2  # both hunks preserved
        assert f.edit_type == EDIT_TYPE.MODIFIED

    def test_rename_without_hunk_gets_synthesized_patch_and_reads_old_path(self):
        """A pure rename has no ``@@`` hunk, so the parser produces no patch. The
        file must NOT be dropped: base content is read from previous_filename and
        a patch is synthesized via load_large_diff."""
        from pr_agent.algo.types import EDIT_TYPE
        git_files = [{
            'filename': 'new_name.py', 'previous_filename': 'old_name.py',
            'status': 'renamed', 'additions': 1, 'deletions': 1,
        }]
        provider = self._provider(
            git_files,
            file_diffs={},  # no patch for the rename
            file_contents={'new_name.py': 'line1\nline2 changed\n'},
        )
        provider.repo_api.get_file_content.return_value = 'line1\nline2\n'  # base (old path)

        with self._patch_valid_file():
            diff_files = provider.get_diff_files()

        assert len(diff_files) == 1
        f = diff_files[0]
        assert f.filename == 'new_name.py'
        assert f.old_filename == 'old_name.py'
        assert f.edit_type == EDIT_TYPE.RENAMED
        # base content must have been read from the OLD path (rename source).
        _, kwargs = provider.repo_api.get_file_content.call_args
        assert kwargs['filepath'] == 'old_name.py'
        # a patch was synthesized (not left empty) since content changed.
        assert f.patch != ''
        assert '@@' in f.patch

    def test_binary_file_is_not_dropped(self):
        """A binary file appears in the files list but has no textual patch; it
        must still be represented (never silently dropped)."""
        from pr_agent.algo.types import EDIT_TYPE
        git_files = [{'filename': 'logo.png', 'status': 'modified', 'additions': 0, 'deletions': 0}]
        provider = self._provider(
            git_files,
            file_diffs={},  # binary -> no hunk
            file_contents={'logo.png': ''},  # binary content decoded to ""
        )
        with self._patch_valid_file():
            diff_files = provider.get_diff_files()

        # The binary file is still present in the diff-files list.
        assert [f.filename for f in diff_files] == ['logo.png']
        assert diff_files[0].edit_type == EDIT_TYPE.MODIFIED

    def test_added_and_deleted_files_get_correct_edit_types(self):
        from pr_agent.algo.types import EDIT_TYPE
        git_files = [
            {'filename': 'new.py', 'status': 'added', 'additions': 3, 'deletions': 0},
            {'filename': 'gone.py', 'status': 'deleted', 'additions': 0, 'deletions': 3},
        ]
        provider = self._provider(
            git_files,
            file_diffs={
                'new.py': '@@ -0,0 +1,3 @@\n+a\n+b\n+c',
                'gone.py': '@@ -1,3 +0,0 @@\n-a\n-b\n-c',
            },
            file_contents={'new.py': 'a\nb\nc\n'},
        )
        with self._patch_valid_file():
            diff_files = provider.get_diff_files()

        by_name = {f.filename: f for f in diff_files}
        assert by_name['new.py'].edit_type == EDIT_TYPE.ADDED
        assert by_name['gone.py'].edit_type == EDIT_TYPE.DELETED

    def test_base_content_read_from_merge_base(self):
        """Base content must be read from the merge base, not the raw base.sha."""
        git_files = [{'filename': 'a.py', 'status': 'modified', 'additions': 1, 'deletions': 0}]
        provider = self._provider(
            git_files,
            file_diffs={'a.py': '@@ -1,1 +1,2 @@\n a\n+b'},
            file_contents={'a.py': 'a\nb\n'},
            base_sha='basesha', merge_base_sha='mergebasesha',
        )
        with self._patch_valid_file():
            provider.get_diff_files()

        _, kwargs = provider.repo_api.get_file_content.call_args
        assert kwargs['commit_sha'] == 'mergebasesha'

    def test_prefers_compare_diff_over_pr_diff(self):
        """When the compare API returns a diff, it is used as the patch source
        (merge-base relative) in preference to the PR .diff."""
        git_files = [{'filename': 'a.py', 'status': 'modified', 'additions': 1, 'deletions': 0}]
        compare_diff = (
            'diff --git a/a.py b/a.py\n'
            'index 111..222 100644\n'
            '--- a/a.py\n'
            '+++ b/a.py\n'
            '@@ -1,1 +1,2 @@\n a\n+from_compare'
        )
        provider = self._provider(
            git_files,
            file_diffs={'a.py': '@@ -1,1 +1,2 @@\n a\n+from_pr_diff'},
            file_contents={'a.py': 'a\nfrom_compare\n'},
            compare_diff=compare_diff,
        )
        with self._patch_valid_file():
            diff_files = provider.get_diff_files()

        assert 'from_compare' in diff_files[0].patch
        assert 'from_pr_diff' not in diff_files[0].patch
        provider.repo_api.get_compare_diff.assert_called_once()

    def test_falls_back_to_pr_diff_when_compare_unavailable(self):
        """If the compare API returns nothing, the PR .diff patch is used —
        never regressing the pre-existing behavior."""
        git_files = [{'filename': 'a.py', 'status': 'modified', 'additions': 1, 'deletions': 0}]
        provider = self._provider(
            git_files,
            file_diffs={'a.py': '@@ -1,1 +1,2 @@\n a\n+from_pr_diff'},
            file_contents={'a.py': 'a\nfrom_pr_diff\n'},
            compare_diff='',  # compare unavailable
        )
        with self._patch_valid_file():
            diff_files = provider.get_diff_files()

        assert 'from_pr_diff' in diff_files[0].patch

    def test_invalid_files_are_filtered(self):
        git_files = [
            {'filename': 'a.py', 'status': 'modified', 'additions': 1, 'deletions': 0},
            {'filename': 'skip.bin', 'status': 'modified', 'additions': 1, 'deletions': 0},
        ]
        provider = self._provider(git_files, file_diffs={'a.py': '@@ -1 +1,2 @@\n a\n+b'})

        def only_py(name):
            return name.endswith('.py')

        with patch('pr_agent.git_providers.gitea_provider.is_valid_file', side_effect=only_py):
            diff_files = provider.get_diff_files()

        assert [f.filename for f in diff_files] == ['a.py']


class TestRepoApiCompare:
    """Tests for the compare HTTP calls used to build merge-base-relative diffs.

    ``GET /repos/{owner}/{repo}/compare/{base}...{head}`` (docs.gitea.com
    repoCompareDiff). JSON form returns ``{total_commits, commits[]}``; the
    ``?output=diff`` form returns the raw unified diff.
    """

    @staticmethod
    def _repo_api():
        from pr_agent.git_providers.gitea_provider import RepoApi

        client = MagicMock()
        return RepoApi(client), client

    def test_get_compare_hits_compare_endpoint_three_dot(self):
        repo_api, client = self._repo_api()
        mock_resp = MagicMock()
        mock_resp.data = BytesIO(b'{"total_commits": 2, "commits": []}')
        client.call_api.return_value = mock_resp

        result = repo_api.get_compare('owner', 'repo', 'base_sha', 'head_sha')

        args, kwargs = client.call_api.call_args
        # three-dot (merge-base relative), base...head embedded in the wildcard path
        assert args[0] == '/repos/owner/repo/compare/base_sha...head_sha'
        assert args[1] == 'GET'
        assert kwargs.get('auth_settings') == ['AuthorizationHeaderToken']
        assert result == {'total_commits': 2, 'commits': []}

    def test_get_compare_returns_empty_dict_on_error(self):
        from giteapy.rest import ApiException
        repo_api, client = self._repo_api()
        client.call_api.side_effect = ApiException(status=404)

        assert repo_api.get_compare('owner', 'repo', 'b', 'h') == {}

    def test_get_compare_diff_requests_output_diff(self):
        repo_api, client = self._repo_api()
        mock_resp = MagicMock()
        mock_resp.data = BytesIO(b'diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b')
        client.call_api.return_value = mock_resp

        diff = repo_api.get_compare_diff('owner', 'repo', 'base_sha', 'head_sha')

        args, kwargs = client.call_api.call_args
        assert args[0] == '/repos/owner/repo/compare/base_sha...head_sha'
        assert ('output', 'diff') in kwargs.get('query_params', [])
        assert diff.startswith('diff --git')

    def test_get_compare_diff_returns_empty_on_error(self):
        from giteapy.rest import ApiException
        repo_api, client = self._repo_api()
        client.call_api.side_effect = ApiException(status=500)

        assert repo_api.get_compare_diff('owner', 'repo', 'b', 'h') == ''


class TestGiteaParseUnifiedDiff:
    """The shared unified-diff parser used by both the PR .diff and compare-diff
    paths. Multi-hunk and multi-file behavior must match the original parser."""

    @staticmethod
    def _parse(text):
        from pr_agent.git_providers.gitea_provider import GiteaProvider
        return GiteaProvider._parse_unified_diff(text)

    def test_multi_hunk_kept(self):
        diff = (
            'diff --git a/f.py b/f.py\n'
            'index 1..2 100644\n--- a/f.py\n+++ b/f.py\n'
            '@@ -1,2 +1,3 @@\n a\n+b\n c\n'
            '@@ -20,2 +21,3 @@\n d\n+e\n f'
        )
        parsed = self._parse(diff)
        assert set(parsed) == {'f.py'}
        assert parsed['f.py'].count('@@ -') == 2
        assert parsed['f.py'].startswith('@@ -1,2 +1,3 @@')

    def test_multiple_files(self):
        diff = (
            'diff --git a/f1.py b/f1.py\n@@ -1 +1,2 @@\n a\n+b\n'
            'diff --git a/f2.py b/f2.py\n@@ -5 +5,2 @@\n c\n+d'
        )
        parsed = self._parse(diff)
        assert set(parsed) == {'f1.py', 'f2.py'}

    def test_rename_with_no_hunk_produces_no_entry(self):
        """A pure rename has no @@ hunk -> no entry (get_diff_files synthesizes
        the patch for it separately, so it is not lost)."""
        diff = (
            'diff --git a/old.py b/new.py\n'
            'similarity index 100%\n'
            'rename from old.py\n'
            'rename to new.py'
        )
        assert self._parse(diff) == {}

    def test_empty_diff(self):
        assert self._parse('') == {}


class TestGiteaProviderIncremental:
    """Incremental review: re-review only the commits pushed since the last
    review (ports github_provider.get_incremental_commits & friends)."""

    @staticmethod
    def _commit(sha, iso_date, message='msg'):
        return {'sha': sha, 'commit': {'message': message, 'author': {'date': iso_date, 'name': 'a'}}}

    @staticmethod
    def _provider(pr_commits=None, comments=None):
        from pr_agent.git_providers.gitea_provider import GiteaProvider
        from pr_agent.git_providers.git_provider import IncrementalPR

        provider = GiteaProvider.__new__(GiteaProvider)
        provider.logger = MagicMock()
        provider.owner = 'owner'
        provider.repo = 'repo'
        provider.pr_number = 1
        provider.enabled_pr = True
        provider.sha = 'headsha'
        provider.base_sha = 'basesha'
        provider.incremental = IncrementalPR(False)
        provider.unreviewed_files_set = {}
        provider.repo_api = MagicMock()
        provider.repo_api.get_pr_commits.return_value = pr_commits or []
        provider.repo_api.list_all_comments.return_value = comments or []
        provider.repo_api.get_compare.return_value = {'commits': []}
        return provider

    def test_get_incremental_commits_noop_for_full_review(self):
        from pr_agent.git_providers.git_provider import IncrementalPR
        provider = self._provider()
        provider.get_incremental_commits(IncrementalPR(False))
        # Full review: no commit lookup, incremental stays off.
        assert provider.incremental.is_incremental is False
        provider.repo_api.get_pr_commits.assert_not_called()

    def test_previous_review_matched_by_header(self):
        from pr_agent.algo.utils import PRReviewHeader
        c1 = MagicMock(); c1.body = 'random chatter'
        c2 = MagicMock(); c2.body = f'{PRReviewHeader.REGULAR.value} 🔍\n\nreview text'
        provider = self._provider(comments=[c1, c2])
        prev = provider.get_previous_review(full=True, incremental=True)
        assert prev is c2

    def test_previous_review_none_when_no_review_comment(self):
        c1 = MagicMock(); c1.body = 'just a comment'
        provider = self._provider(comments=[c1])
        assert provider.get_previous_review(full=True, incremental=True) is None

    def test_no_previous_review_falls_back_to_full(self):
        import datetime
        commits = [
            self._commit('sha1', '2024-01-01T00:00:00Z'),
            self._commit('sha2', '2024-01-02T00:00:00Z'),
        ]
        from pr_agent.git_providers.git_provider import IncrementalPR
        provider = self._provider(pr_commits=commits, comments=[])  # no review comment
        with patch('pr_agent.git_providers.gitea_provider.get_settings') as gs:
            gs.return_value.get.return_value = None  # no push before-sha
            provider.get_incremental_commits(IncrementalPR(True))
        # Falls back to a full review.
        assert provider.incremental.is_incremental is False

    def test_incremental_commit_range_after_previous_review(self):
        import datetime
        from pr_agent.git_providers.git_provider import IncrementalPR
        # Two commits: one before the review, one after.
        commits = [
            self._commit('old', '2024-01-01T00:00:00Z'),
            self._commit('new', '2024-01-03T00:00:00Z'),
        ]
        review = MagicMock()
        from pr_agent.algo.utils import PRReviewHeader
        review.body = f'{PRReviewHeader.REGULAR.value} 🔍'
        review.created_at = datetime.datetime(2024, 1, 2, tzinfo=datetime.timezone.utc)
        provider = self._provider(pr_commits=commits, comments=[review])
        provider.repo_api.get_compare.return_value = {
            'commits': [{'files': [{'filename': 'changed.py', 'status': 'modified'}]}]
        }
        with patch('pr_agent.git_providers.gitea_provider.get_settings') as gs:
            gs.return_value.get.return_value = None
            provider.get_incremental_commits(IncrementalPR(True))

        assert provider.incremental.is_incremental is True
        # Only the commit after the review is "new".
        assert [c.sha for c in provider.incremental.commits_range] == ['new']
        assert provider.incremental.first_new_commit_sha == 'new'
        assert provider.incremental.last_seen_commit_sha == 'old'
        # The compare range is diffed to collect changed files.
        assert 'changed.py' in provider.unreviewed_files_set
        _, kwargs = provider.repo_api.get_compare.call_args
        assert kwargs['base'] == 'old'
        assert kwargs['head'] == 'headsha'

    def test_push_before_sha_anchors_incremental_without_prior_review(self):
        """No prior review comment, but the push webhook gave a before-SHA: the
        review still runs incrementally, anchored to that SHA."""
        from pr_agent.git_providers.git_provider import IncrementalPR
        commits = [
            self._commit('c0', '2024-01-01T00:00:00Z'),
            self._commit('c1', '2024-01-02T00:00:00Z'),
            self._commit('c2', '2024-01-03T00:00:00Z'),
        ]
        provider = self._provider(pr_commits=commits, comments=[])
        provider.repo_api.get_compare.return_value = {
            'commits': [{'files': [{'filename': 'f.py', 'status': 'modified'}]}]
        }
        with patch('pr_agent.git_providers.gitea_provider.get_settings') as gs:
            gs.return_value.get.side_effect = lambda k, d=None: (
                'c0' if k == 'gitea.incremental_push_before_sha' else d
            )
            provider.get_incremental_commits(IncrementalPR(True))

        assert provider.incremental.is_incremental is True
        assert provider.incremental.last_seen_commit_sha == 'c0'
        # commits after c0 are the new ones.
        assert [c.sha for c in provider.incremental.commits_range] == ['c1', 'c2']
        assert 'f.py' in provider.unreviewed_files_set

    def test_incremental_get_diff_files_restricts_to_new_files_and_uses_last_seen_base(self):
        """In incremental mode get_diff_files reviews only the unreviewed files
        and reads base content from the last-seen commit, not the repo head."""
        from pr_agent.git_providers.gitea_provider import GiteaProvider
        from pr_agent.git_providers.git_provider import IncrementalPR

        provider = GiteaProvider.__new__(GiteaProvider)
        provider.logger = MagicMock()
        provider.owner = 'owner'; provider.repo = 'repo'; provider.pr_number = 1
        provider.enabled_pr = True
        provider.sha = 'headsha'; provider.base_sha = 'basesha'; provider.merge_base_sha = 'mbsha'
        provider.git_files = [
            {'filename': 'new.py', 'status': 'modified', 'additions': 1, 'deletions': 0},
            {'filename': 'untouched.py', 'status': 'modified', 'additions': 1, 'deletions': 0},
        ]
        provider.file_diffs = {'new.py': '@@ -1 +1,2 @@\n a\n+b'}
        provider.file_contents = {'new.py': 'a\nb\n'}
        provider.diff_files = []
        inc = IncrementalPR(True)
        inc.last_seen_commit = __import__('types').SimpleNamespace(sha='lastseen')
        provider.incremental = inc
        provider.unreviewed_files_set = {'new.py': 'new.py'}  # only new.py is new
        provider.repo_api = MagicMock()
        provider.repo_api.get_compare_diff.return_value = ''  # fall back to file_diffs
        provider.repo_api.get_file_content.return_value = 'a\n'  # base content

        with patch('pr_agent.git_providers.gitea_provider.is_valid_file', return_value=True):
            diff_files = provider.get_diff_files()

        # Only the unreviewed file is reviewed.
        assert [f.filename for f in diff_files] == ['new.py']
        # Base content was read from the last-seen commit, not the repo head.
        _, kwargs = provider.repo_api.get_file_content.call_args
        assert kwargs['commit_sha'] == 'lastseen'
