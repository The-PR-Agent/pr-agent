import pytest

from pr_agent.git_providers.git_provider import GitProvider


def test_find_open_pr_url_is_not_implemented_by_default():
    with pytest.raises(NotImplementedError):
        GitProvider.find_open_pr_url(
            object(), "org/repo", "contributor/repo", "feature/fork-review", "abc123"
        )
