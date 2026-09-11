"""The default `IncrementalPR` is per call, not one object shared since import."""
from unittest.mock import MagicMock, patch

from pr_agent.git_providers.git_provider import IncrementalPR
from pr_agent.git_providers.github_provider import GithubProvider


def _provider() -> GithubProvider:
    with (
        patch("pr_agent.git_providers.github_provider.get_settings") as get_settings,
        patch.object(GithubProvider, "_get_github_client", return_value=MagicMock()),
    ):
        get_settings.return_value.get.side_effect = lambda _key, default=None: default
        return GithubProvider(pr_url=None)


def test_two_providers_do_not_share_one_default_incremental():
    """A long-lived server handles many pull requests through one process."""
    first, second = _provider(), _provider()

    first.get_incremental_commits()
    second.get_incremental_commits()

    assert first.incremental is not second.incremental

    # The writes the incremental path makes, on the object the default supplied.
    first.incremental.is_incremental = True
    first.incremental.commits_range = ["c1"]
    first.incremental.last_seen_commit = "sha"

    assert second.incremental.is_incremental is False
    assert second.incremental.commits_range is None
    assert second.incremental.last_seen_commit is None


def test_two_calls_on_one_provider_do_not_share_it_either():
    provider = _provider()

    provider.get_incremental_commits()
    first = provider.incremental
    first.commits_range = ["c1"]

    provider.get_incremental_commits()

    assert provider.incremental is not first
    assert provider.incremental.commits_range is None


def test_an_incremental_passed_in_is_the_one_used():
    """The fix must not replace what a caller handed over: `/review -i` reads
    `commits_range` back off the object it passed."""
    provider = _provider()
    given = IncrementalPR(False)

    provider.get_incremental_commits(given)

    assert provider.incremental is given
