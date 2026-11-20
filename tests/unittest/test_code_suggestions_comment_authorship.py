from unittest.mock import MagicMock

import pytest

from pr_agent.algo.comment_identity import comment_authorship_available
from pr_agent.commands.pr_reviewer import PRReviewer


@pytest.mark.parametrize(("state_support", "comment_support", "expected"), [
    (True, True, True),
    (False, True, False),
    (True, False, False),
    # Mocks and loosely typed providers return truthy objects; only an explicit True counts.
    (MagicMock(), True, False),
    (RuntimeError("unavailable"), True, False),
])
def test_review_and_suggestions_share_the_authorship_gate(state_support, comment_support, expected):
    provider = MagicMock()
    if isinstance(state_support, Exception):
        provider.supports_review_finding_state.side_effect = state_support
    else:
        provider.supports_review_finding_state.return_value = state_support
    provider.is_supported.return_value = comment_support
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider

    assert comment_authorship_available(provider) is expected
    assert reviewer._review_comment_authorship_available() is expected


def test_authorship_gate_without_a_provider():
    assert comment_authorship_available(None) is False
    assert PRReviewer.__new__(PRReviewer)._review_comment_authorship_available() is False
