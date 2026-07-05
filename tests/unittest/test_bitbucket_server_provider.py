from unittest.mock import MagicMock

import pytest
from atlassian.bitbucket import Bitbucket
from packaging.version import parse as parse_version

from pr_agent.git_providers.bitbucket_server_provider import BitbucketServerProvider


class TestParsePrUrl:
    @pytest.mark.parametrize("url, expected", [
        (
            "https://git.example.com/projects/AAA/repos/my-repo/pull-requests/1",
            ("AAA", "my-repo", 1),
        ),
        (  # context path before /projects/ (e.g. Bitbucket served under a prefix)
            "https://git.example.com/bitbucket/projects/AAA/repos/my-repo/pull-requests/5",
            ("AAA", "my-repo", 5),
        ),
        (  # personal repository form maps to a "~user" workspace
            "https://git.example.com/users/john.doe/repos/my-repo/pull-requests/7",
            ("~john.doe", "my-repo", 7),
        ),
        (  # trailing path segments after the PR number are tolerated
            "https://git.example.com/projects/AAA/repos/my-repo/pull-requests/42/overview",
            ("AAA", "my-repo", 42),
        ),
        (
            "https://git.example.com/projects/AAA/repos/my-repo/pull-requests/321?diff",
            ("AAA", "my-repo", 321),
        ),
    ])
    def test_valid_urls(self, url, expected):
        assert BitbucketServerProvider._parse_pr_url(url) == expected

    @pytest.mark.parametrize("url", [
        # neither /projects/ nor /users/ in the path
        "https://git.example.com/AAA/repos/my-repo/pull-requests/1",
        # wrong segment where "repos" is expected
        "https://git.example.com/projects/AAA/repositories/my-repo/pull-requests/1",
        # missing the pull-requests segment
        "https://git.example.com/projects/AAA/repos/my-repo",
        # wrong segment where "pull-requests" is expected
        "https://git.example.com/projects/AAA/repos/my-repo/pulls/1",
        # a plain bitbucket.org (cloud) URL
        "https://bitbucket.org/WORKSPACE_XYZ/MY_TEST_REPO/pull-requests/321",
    ])
    def test_invalid_urls_raise_value_error(self, url):
        with pytest.raises(ValueError):
            BitbucketServerProvider._parse_pr_url(url)

    def test_non_integer_pr_number_raises_value_error(self):
        url = "https://git.example.com/projects/AAA/repos/my-repo/pull-requests/abc"
        with pytest.raises(ValueError, match="Unable to convert PR number"):
            BitbucketServerProvider._parse_pr_url(url)


class TestParseBitbucketServer:
    @pytest.mark.parametrize("url, expected", [
        (
            "https://git.example.com/projects/AAA/repos/my-repo/pull-requests/1",
            "https://git.example.com",
        ),
        (  # the context path before /projects/ is preserved
            "https://git.example.com/bitbucket/projects/AAA/repos/my-repo/pull-requests/1",
            "https://git.example.com/bitbucket",
        ),
        (  # no /projects/ segment: fall back to scheme + netloc
            "https://git.example.com/users/john/repos/r/pull-requests/1",
            "https://git.example.com",
        ),
        ("https://git.example.com", "https://git.example.com"),
    ])
    def test_parse_bitbucket_server(self, url, expected):
        assert BitbucketServerProvider._parse_bitbucket_server(url) == expected


class TestCapabilities:
    @pytest.mark.parametrize("capability", [
        "get_issue_comments",
        "get_labels",
        "gfm_markdown",
        "publish_file_comments",
    ])
    def test_unsupported_capabilities(self, capability):
        provider = BitbucketServerProvider.__new__(BitbucketServerProvider)
        assert provider._is_supported(capability) is False

    @pytest.mark.parametrize("capability", ["create_inline_comment", "publish_description"])
    def test_supported_capabilities(self, capability):
        provider = BitbucketServerProvider.__new__(BitbucketServerProvider)
        assert provider._is_supported(capability) is True


class TestApiVersionDetection:
    """__init__ probes rest/api/1.0/application-properties to learn the server version."""

    def _client(self):
        client = MagicMock(Bitbucket)
        client.url = "https://git.example.com"
        return client

    def test_api_version_parsed_from_application_properties(self):
        client = self._client()
        client.get.return_value = {"version": "8.16"}

        provider = BitbucketServerProvider(bitbucket_client=client)

        assert provider.bitbucket_api_version == parse_version("8.16")
        client.get.assert_called_once_with("rest/api/1.0/application-properties")

    def test_api_version_is_none_when_probe_fails(self):
        client = self._client()
        client.get.side_effect = ConnectionError("no network")

        provider = BitbucketServerProvider(bitbucket_client=client)

        assert provider.bitbucket_api_version is None

    def test_server_url_taken_from_client_when_provided(self):
        client = self._client()
        client.get.return_value = {"version": "7.0"}

        provider = BitbucketServerProvider(bitbucket_client=client)

        assert provider.bitbucket_server_url == "https://git.example.com"


class TestUrlHelpers:
    def _provider(self):
        provider = BitbucketServerProvider.__new__(BitbucketServerProvider)
        provider.bitbucket_server_url = "https://git.example.com"
        provider.workspace_slug = "AAA"
        provider.repo_slug = "My-Repo"
        provider.pr_num = 1
        provider.pr_url = "https://git.example.com/projects/AAA/repos/My-Repo/pull-requests/1"
        return provider

    def test_get_git_repo_url_lowercases_slugs(self):
        provider = self._provider()
        assert provider.get_git_repo_url() == "https://git.example.com/scm/aaa/my-repo.git"

    def test_get_canonical_url_parts_from_git_url(self):
        provider = self._provider()
        prefix, suffix = provider.get_canonical_url_parts(
            repo_git_url="https://git.example.com/scm/my_work/my_repo.git",
            desired_branch="my_branch",
        )
        assert prefix == "https://git.example.com/projects/my_work/repos/my_repo/browse"
        assert suffix == "?at=refs%2Fheads%2Fmy_branch"

    def test_get_canonical_url_parts_with_malformed_git_url(self):
        provider = self._provider()
        prefix, suffix = provider.get_canonical_url_parts(
            repo_git_url="https://git.example.com/no-scm/my_repo.git",
            desired_branch="main",
        )
        assert (prefix, suffix) == ("", "")

    def test_get_line_link_with_line_number(self):
        provider = self._provider()
        link = provider.get_line_link("src/main.py", 12)
        assert link == f"{provider.pr_url}/diff#src%2Fmain.py?t=12"

    def test_get_line_link_without_line_number(self):
        provider = self._provider()
        link = provider.get_line_link("src/main.py", -1)
        assert link == f"{provider.pr_url}/diff#src%2Fmain.py"

    def test_pr_comments_path_and_merge_base_paths(self):
        provider = self._provider()
        assert provider._get_pr_comments_path() == (
            "rest/api/latest/projects/AAA/repos/My-Repo/pull-requests/1/comments"
        )
        assert provider._get_merge_base() == (
            "rest/api/latest/projects/AAA/repos/My-Repo/pull-requests/1/merge-base"
        )


class TestGetBestCommonAncestor:
    def test_parent_found_in_destination_commits(self):
        source_commits = [
            {"id": "s2", "parents": [{"id": "s1"}]},
            {"id": "s1", "parents": [{"id": "d2"}]},
        ]
        destination_commits = [{"id": "d3"}, {"id": "d2"}, {"id": "d1"}]
        result = BitbucketServerProvider.get_best_common_ancestor(
            source_commits, destination_commits, "d0"
        )
        assert result == "d2"

    def test_guaranteed_ancestor_matches_directly(self):
        source_commits = [{"id": "s1", "parents": [{"id": "d0"}]}]
        result = BitbucketServerProvider.get_best_common_ancestor(source_commits, [], "d0")
        assert result == "d0"

    def test_falls_back_to_guaranteed_ancestor(self):
        source_commits = [{"id": "s1", "parents": [{"id": "unrelated"}]}]
        destination_commits = [{"id": "d1"}]
        result = BitbucketServerProvider.get_best_common_ancestor(
            source_commits, destination_commits, "d0"
        )
        assert result == "d0"


class TestPublishCodeSuggestions:
    def _provider_with_captured_comments(self):
        provider = BitbucketServerProvider.__new__(BitbucketServerProvider)
        captured = []
        provider.publish_inline_comments = captured.extend
        return provider, captured

    def test_single_line_suggestion_payload(self):
        provider, captured = self._provider_with_captured_comments()
        ok = provider.publish_code_suggestions([{
            "body": "use a constant",
            "relevant_file": "src/main.py",
            "relevant_lines_start": 10,
            "relevant_lines_end": 10,
        }])
        assert ok is True
        assert captured == [{
            "body": "use a constant",
            "path": "src/main.py",
            "line": 10,
            "side": "RIGHT",
        }]

    def test_multi_line_suggestion_uses_start_line_and_code_block(self):
        provider, captured = self._provider_with_captured_comments()
        ok = provider.publish_code_suggestions([{
            "body": "```suggestion\nnew code\n```",
            "relevant_file": "src/main.py",
            "relevant_lines_start": 5,
            "relevant_lines_end": 8,
        }])
        assert ok is True
        assert len(captured) == 1
        comment = captured[0]
        assert comment["start_line"] == 5
        assert comment["line"] == 8
        assert comment["start_side"] == "RIGHT"
        # multi-line suggestions are demoted to a plain code block
        assert "```suggestion" not in comment["body"]
        assert comment["body"].startswith("```")

    @pytest.mark.parametrize("start, end", [
        (-1, 5),   # invalid start line
        (0, 5),    # missing start line
        (10, 5),   # end before start
    ])
    def test_invalid_line_ranges_are_skipped(self, start, end):
        provider, captured = self._provider_with_captured_comments()
        ok = provider.publish_code_suggestions([{
            "body": "something",
            "relevant_file": "src/main.py",
            "relevant_lines_start": start,
            "relevant_lines_end": end,
        }])
        assert ok is True
        assert captured == []


class TestPublishInlineComments:
    def _provider_with_mocked_inline_comment(self):
        provider = BitbucketServerProvider.__new__(BitbucketServerProvider)
        provider.publish_inline_comment = MagicMock()
        return provider

    def test_dispatch_by_position(self):
        provider = self._provider_with_mocked_inline_comment()
        provider.publish_inline_comments([{"body": "b", "position": 3, "path": "f.py"}])
        provider.publish_inline_comment.assert_called_once_with("b", 3, "f.py")

    def test_dispatch_multi_line_uses_start_line(self):
        provider = self._provider_with_mocked_inline_comment()
        provider.publish_inline_comments(
            [{"body": "b", "start_line": 2, "line": 4, "path": "f.py"}]
        )
        provider.publish_inline_comment.assert_called_once_with("b", 2, "f.py")

    def test_dispatch_single_line(self):
        provider = self._provider_with_mocked_inline_comment()
        provider.publish_inline_comments([{"body": "b", "line": 7, "path": "f.py"}])
        provider.publish_inline_comment.assert_called_once_with("b", 7, "f.py")
