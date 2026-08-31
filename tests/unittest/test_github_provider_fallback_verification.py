from unittest.mock import MagicMock, patch

from pr_agent.git_providers.github_provider import GithubProvider


def test_fallback_logs_dropped_comments_when_fix_fails():
    provider = GithubProvider.__new__(GithubProvider)
    provider.publish_inline_comments = MagicMock()
    provider.pr = MagicMock()
    provider.last_commit_id = "abc"

    comment_1 = {"path": "f1.py", "body": "```suggestion\nfixed\n```"}
    comment_2 = {"path": "f2.py", "body": "invalid"}

    # Mock verify to return 0 verified, 2 invalid
    provider._verify_code_comments = MagicMock(
        return_value=([], [(comment_1, Exception("e1")), (comment_2, Exception("e2"))])
    )

    # Mock fix to return only the first one
    fixed_1 = {"path": "f1.py", "body": "fixed"}
    provider._try_fix_invalid_inline_comments = MagicMock(return_value=[fixed_1])

    with (
        patch("pr_agent.git_providers.github_provider.get_logger") as mock_logger,
        patch("pr_agent.git_providers.github_provider.get_settings") as mock_settings,
    ):
        mock_settings.return_value.github.try_fix_invalid_inline_comments = True

        provider._publish_inline_comments_fallback_with_verification([comment_1, comment_2])

        # Verify one dropped-comment warning.
        mock_logger.return_value.warning.assert_called_with(
            "Dropped 1 invalid comments that could not be fixed. Paths: ['f2.py']"
        )


def test_fallback_logs_dropped_comments_when_fix_disabled():
    provider = GithubProvider.__new__(GithubProvider)
    provider.publish_inline_comments = MagicMock()
    provider.pr = MagicMock()
    provider.last_commit_id = "abc"

    comment_1 = {"path": "f1.py", "body": "invalid"}
    comment_2 = {"path": "f2.py", "body": "invalid"}

    # Mock verify to return 0 verified, 2 invalid
    provider._verify_code_comments = MagicMock(
        return_value=([], [(comment_1, Exception("e1")), (comment_2, Exception("e2"))])
    )

    with (
        patch("pr_agent.git_providers.github_provider.get_logger") as mock_logger,
        patch("pr_agent.git_providers.github_provider.get_settings") as mock_settings,
    ):
        mock_settings.return_value.github.try_fix_invalid_inline_comments = False

        provider._publish_inline_comments_fallback_with_verification([comment_1, comment_2])

        # Verify two dropped-comment warnings.
        mock_logger.return_value.warning.assert_called_with(
            "Dropped 2 invalid comments "
            "(try_fix_invalid_inline_comments is off). Paths: ['f1.py', 'f2.py']"
        )
