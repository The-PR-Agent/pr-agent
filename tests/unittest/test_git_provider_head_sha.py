# SPDX-License-Identifier: Apache-2.0
"""Tests for the provider-level PR head SHA contract.

`persistent_finding_state` resolves a previously reported finding when the head moved
since the last full review. The reviewer used to read a GitHub-shaped `last_commit_id`
attribute directly, so providers that never set it recorded an empty head and could
never resolve anything. Each provider now resolves its own shape behind
`get_pr_head_sha()`, which the reviewer calls.
"""

from types import SimpleNamespace

from pr_agent.git_providers.azuredevops_provider import AzureDevopsProvider
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider
from pr_agent.git_providers.bitbucket_server_provider import BitbucketServerProvider
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.gitea_provider import GiteaProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider
from pr_agent.tools.pr_reviewer import PRReviewer

HEAD_SHA = "a" * 40

# (name, provider class, attribute holding its PR object, the object shape it exposes)
PROVIDER_SHAPES = (
    ("gitlab", GitLabProvider, "mr", SimpleNamespace(diff_refs={"head_sha": HEAD_SHA})),
    ("bitbucket", BitbucketProvider, "pr", SimpleNamespace(data={"source": {"commit": {"hash": HEAD_SHA}}})),
    ("bitbucket-server", BitbucketServerProvider, "pr", SimpleNamespace(fromRef={"latestCommit": HEAD_SHA})),
    ("github", GithubProvider, "pr", SimpleNamespace(head=SimpleNamespace(sha=HEAD_SHA))),
    ("gitea", GiteaProvider, "last_commit", SimpleNamespace(sha=HEAD_SHA)),
    # Azure is the provider #3431 was filed against. It records the source
    # revision on `last_merge_commit`, a different field from every other
    # provider here, so it has its own row.
    ("azure", AzureDevopsProvider, "pr", SimpleNamespace(last_merge_commit=SimpleNamespace(commit_id=HEAD_SHA))),
)


def _provider(cls, attribute, shape):
    provider = cls.__new__(cls)
    setattr(provider, attribute, shape)
    return provider


def _reviewer(provider):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider
    return reviewer


def test_providers_resolve_their_own_head_sha_shape():
    """Resolve each provider's head through the field its own API exposes."""
    for name, cls, attribute, shape in PROVIDER_SHAPES:
        provider = _provider(cls, attribute, shape)
        assert provider.get_pr_head_sha() == HEAD_SHA, name


def test_review_head_sha_reads_the_provider_hook():
    """Resolve a head for every provider in the matrix, whatever its API shape."""
    for name, cls, attribute, shape in PROVIDER_SHAPES:
        reviewer = _reviewer(_provider(cls, attribute, shape))
        assert reviewer._review_head_sha() == HEAD_SHA, name


def test_unresolvable_head_stays_empty_rather_than_guessing():
    """Refuse to invent a head for a provider that has none to report.

    The reconciliation guard only resolves findings when both the previous and the
    current head are non-empty and different, so an empty head safely refuses.
    """
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.mr = SimpleNamespace(diff_refs={})
    assert provider.get_pr_head_sha() == ""
    assert _reviewer(provider)._review_head_sha() == ""

    # A malformed payload degrades to empty instead of raising into the review run.
    provider.mr = SimpleNamespace(diff_refs={"head_sha": 12345})
    assert provider.get_pr_head_sha() == ""
    provider.mr = SimpleNamespace()
    assert provider.get_pr_head_sha() == ""


def test_base_contract_defaults_to_empty():
    """Pin the base method as a no-op so a new provider opts in explicitly."""
    import inspect

    source = inspect.getsource(GitProvider.get_pr_head_sha)
    returns_empty = 'return ""' in source

    assert returns_empty
    # And no provider is left relying on the base by accident: each one that can
    # resolve a head declares the hook itself.
    for name, cls, _, _ in PROVIDER_SHAPES:
        assert cls.get_pr_head_sha is not GitProvider.get_pr_head_sha, name


def test_head_sha_falls_back_when_the_hook_is_absent():
    """Resolve the head for a provider that does not define the hook."""
    for last_commit_id in ("legacy-sha", SimpleNamespace(sha="legacy-sha"), SimpleNamespace(id="legacy-sha")):
        assert _reviewer(SimpleNamespace(last_commit_id=last_commit_id))._review_head_sha() == "legacy-sha"


def test_head_sha_is_empty_when_nothing_resolves():
    """Leave the head empty rather than stale when nothing resolves."""
    assert _reviewer(SimpleNamespace(last_commit_id=None))._review_head_sha() == ""
