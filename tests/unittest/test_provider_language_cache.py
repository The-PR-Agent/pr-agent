"""Pin how many provider round trips get_languages() costs per command.

Count calls on the client stub rather than comparing returned values, because a provider
that kept refetching would still return the right dictionary and a value-only assertion
would keep passing.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from starlette_context import context, request_cycle_context

from pr_agent.git_providers.azuredevops_provider import AzureDevopsProvider
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider
from pr_agent.git_providers.bitbucket_server_provider import BitbucketServerProvider
from pr_agent.git_providers.codecommit_provider import CodeCommitProvider
from pr_agent.git_providers.gerrit_provider import GerritProvider
from pr_agent.git_providers.gitea_provider import GiteaProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.local_git_provider import LocalGitProvider

LANGUAGES = {"Python": 75.0, "JavaScript": 25.0}


class _FakeTree:
    def __init__(self, paths):
        self._paths = paths

    def traverse(self):
        return [SimpleNamespace(path=path, type="blob") for path in self._paths]


class _FakeGitRepo:
    """Stand-in for a GitPython repo that counts full-tree walks."""

    def __init__(self, paths):
        self._tree = _FakeTree(paths)
        self.tree_calls = 0

    def tree(self):
        self.tree_calls += 1
        return self._tree


def _github_provider(languages=LANGUAGES):
    provider = GithubProvider.__new__(GithubProvider)
    repo = SimpleNamespace(get_languages=MagicMock(return_value=languages))
    provider._get_repo = MagicMock(return_value=repo)
    return provider, repo


def _azure_provider(items):
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    provider.workspace_slug = "proj"
    provider.repo_slug = "repo"
    provider.pr = SimpleNamespace(last_merge_target_commit=SimpleNamespace(commit_id="base-sha"))
    provider.azure_devops_client = MagicMock()
    provider.azure_devops_client.get_items.return_value = items
    return provider, provider.azure_devops_client


def _gitea_provider(languages=LANGUAGES):
    provider = GiteaProvider.__new__(GiteaProvider)
    provider.owner = "owner"
    provider.repo = "repo"
    provider.repo_api = SimpleNamespace(get_languages=MagicMock(return_value=languages))
    return provider, provider.repo_api


def _bitbucket_provider(repo_language="python"):
    provider = BitbucketProvider.__new__(BitbucketProvider)
    repo = SimpleNamespace(get_data=MagicMock(return_value=repo_language))
    provider._get_repo = MagicMock(return_value=repo)
    return provider, repo


def _bitbucket_server_provider(files=("a.py",), change_type=None):
    provider = BitbucketServerProvider.__new__(BitbucketServerProvider)
    provider.workspace_slug = "ws"
    provider.repo_slug = "repo"
    provider.pr_num = 7
    changes = [
        {"path": {"toString": path}, **({"type": change_type} if change_type else {})}
        for path in files
    ]
    client = SimpleNamespace(get_pull_requests_changes=MagicMock(return_value=changes))
    provider.bitbucket_client = client
    return provider, client


def _codecommit_provider(files=("a.py",)):
    provider = CodeCommitProvider.__new__(CodeCommitProvider)
    provider.repo_name = "repo"
    provider.get_files = MagicMock(return_value=[SimpleNamespace(filename=f) for f in files])
    return provider, provider.get_files


def _gerrit_provider(paths=("a.py", "src/b.js")):
    provider = GerritProvider.__new__(GerritProvider)
    # __del__ calls cleanup(), which reads repo_path; without it every test logs a
    # spurious cleanup failure at DEBUG.
    provider.repo_path = None
    provider.repo = _FakeGitRepo(paths)
    return provider, provider.repo


def _local_git_provider(paths=("a.py", "src/b.js")):
    provider = LocalGitProvider.__new__(LocalGitProvider)
    provider.repo = _FakeGitRepo(paths)
    return provider, provider.repo


REST_PROVIDERS = [
    pytest.param(_github_provider, "get_languages", id="github"),
    pytest.param(_gitea_provider, "get_languages", id="gitea"),
]

TREE_PROVIDERS = [
    pytest.param(_gerrit_provider, id="gerrit"),
    pytest.param(_local_git_provider, id="local-git"),
]


@pytest.mark.parametrize("factory,method_name", REST_PROVIDERS)
def test_rest_provider_get_languages_fetches_once(factory, method_name):
    provider, client = factory()

    first = provider.get_languages()
    second = provider.get_languages()

    assert first == LANGUAGES
    assert second is first
    assert getattr(client, method_name).call_count == 1


@pytest.mark.parametrize("factory", TREE_PROVIDERS)
def test_git_provider_get_languages_walks_tree_once(factory):
    provider, repo = factory()

    first = provider.get_languages()
    second = provider.get_languages()

    assert first == second
    assert set(first) == {"Python", "JavaScript"}
    assert repo.tree_calls == 1


def test_azure_get_languages_enumerates_repository_once():
    provider, client = _azure_provider([
        SimpleNamespace(git_object_type="blob", path="a.py"),
        SimpleNamespace(git_object_type="blob", path="b.js"),
    ])

    first = provider.get_languages()
    second = provider.get_languages()

    assert first == second
    assert second is first
    assert client.get_items.call_count == 1


def test_bitbucket_get_languages_reads_repository_once():
    provider, repo = _bitbucket_provider()

    first = provider.get_languages()
    second = provider.get_languages()

    assert first == {"python": 0}
    assert second is first
    assert repo.get_data.call_count == 1


@pytest.mark.parametrize("factory", [
    pytest.param(_github_provider, id="github"),
    pytest.param(_gitea_provider, id="gitea"),
])
def test_rest_provider_get_languages_retries_empty_results(factory):
    # RepoApi.get_languages() swallows provider errors and reports them as {}, so caching
    # that empty answer would freeze a transient failure for the rest of the command.
    provider, repo_api = factory()
    repo_api.get_languages = MagicMock(side_effect=[{}, LANGUAGES])

    assert provider.get_languages() == {}
    assert provider.get_languages() == LANGUAGES
    assert provider.get_languages() == LANGUAGES
    assert repo_api.get_languages.call_count == 2


def test_bitbucket_server_get_languages_and_files_share_one_fetch():
    provider, client = _bitbucket_server_provider()

    assert provider.get_files() == ["a.py"]
    assert provider.get_languages() == {"Python": 100.0}
    assert provider.get_files() == ["a.py"]
    assert client.get_pull_requests_changes.call_count == 1


def test_bitbucket_server_get_languages_is_memoized():
    # Caching the change list alone would keep get_pull_requests_changes at one call, so
    # identity is what separates a memoized get_languages() from a recomputed one.
    provider, _ = _bitbucket_server_provider()

    first = provider.get_languages()
    second = provider.get_languages()

    assert first == {"Python": 100.0}
    assert second is first


def test_codecommit_get_languages_is_memoized():
    provider, files = _codecommit_provider()

    first = provider.get_languages()
    second = provider.get_languages()

    assert first == {"Python": 100.0}
    assert second is first
    assert files.call_count == 1


def test_codecommit_set_pr_drops_language_cache():
    provider, _ = _codecommit_provider()
    provider._languages = LANGUAGES
    provider._parse_pr_url = MagicMock(return_value=("repo", 3))
    provider._region_from_valid_pr_url = MagicMock(return_value="us-east-1")
    provider._get_pr_from_client = MagicMock(return_value=MagicMock())
    provider.diff_files = [object()]
    provider.git_files = [object()]

    provider.set_pr("https://us-east-1.console.aws.amazon.com/codesuite/codecommit/repositories/repo/pull-requests/3")

    assert provider._languages is None
    assert provider.diff_files is None
    assert provider.git_files is None


@pytest.mark.parametrize("factory", [
    pytest.param(_github_provider, id="github"),
    pytest.param(_gitea_provider, id="gitea"),
])
def test_rest_provider_get_languages_does_not_cache_failures(factory):
    # A provider that stored a half-finished answer would keep serving it after a
    # transient provider error, so the retry has to reach the client again.
    provider, client = factory()
    client.get_languages.side_effect = RuntimeError("temporary")

    for _ in range(2):
        with pytest.raises(RuntimeError, match="temporary"):
            provider.get_languages()

    assert client.get_languages.call_count == 2


@pytest.mark.parametrize("factory", TREE_PROVIDERS)
def test_git_provider_get_languages_does_not_cache_failures(factory):
    provider, repo = factory()
    repo._tree.traverse = MagicMock(side_effect=RuntimeError("temporary"))

    for _ in range(2):
        with pytest.raises(RuntimeError, match="temporary"):
            provider.get_languages()

    assert repo._tree.traverse.call_count == 2


def test_azure_get_languages_does_not_cache_failures():
    provider, client = _azure_provider([SimpleNamespace(git_object_type="blob", path="a.py")])
    client.get_items.side_effect = RuntimeError("temporary")

    for _ in range(2):
        with pytest.raises(RuntimeError, match="temporary"):
            provider.get_languages()

    assert client.get_items.call_count == 2


def test_bitbucket_get_languages_does_not_cache_failures():
    provider, repo = _bitbucket_provider()
    repo.get_data.side_effect = RuntimeError("temporary")

    for _ in range(2):
        with pytest.raises(RuntimeError, match="temporary"):
            provider.get_languages()

    assert repo.get_data.call_count == 2


def test_github_set_pr_drops_language_cache_for_a_different_pull_request():
    # Retargeting the provider has to invalidate a repository-scoped answer, otherwise a
    # reused instance would keep prioritizing against the previous repository.
    provider, _ = _github_provider()
    provider.repo = "old/repo"
    provider.pr_num = 1
    provider._languages = LANGUAGES
    provider._parse_pr_url = MagicMock(return_value=("new/repo", 2))
    provider._get_pr = MagicMock(return_value=MagicMock())

    provider.set_pr("https://github.com/new/repo/pull/2")

    assert provider._languages is None


def test_github_set_pr_keeps_language_cache_for_the_same_pull_request():
    provider, _ = _github_provider()
    provider.repo = "same/repo"
    provider.pr_num = 7
    provider._languages = LANGUAGES
    provider._parse_pr_url = MagicMock(return_value=("same/repo", 7))
    provider._get_pr = MagicMock(return_value=MagicMock())

    provider.set_pr("https://github.com/same/repo/pull/7")

    assert provider._languages is LANGUAGES


def test_bitbucket_server_get_diff_files_shares_the_cached_change_list():
    # get_diff_files() used to pull its own copy of the change list; assert it now reads
    # the same cached one instead of re-issuing the paginated request.
    provider, client = _bitbucket_server_provider(files=("a.py",), change_type="MODIFY")
    provider.diff_files = None
    provider.unreviewed_files_map = {}
    provider.incremental = SimpleNamespace(is_incremental=False)
    provider.bitbucket_api_version = None
    provider.pr_url = "https://bitbucket.example.com/projects/ws/repos/repo/pull-requests/7"
    provider.pr = SimpleNamespace(
        fromRef={"latestCommit": "head-sha"},
        toRef={"latestCommit": "target-sha"},
        title="edit a",
    )
    provider.bitbucket_client.get_pull_requests_commits = MagicMock(
        return_value=[{"id": "c0", "parents": [{"id": "base-sha"}]}]
    )
    provider.get_file = MagicMock(return_value=b"content\n")

    # Populate the change list through get_files() first, so a get_diff_files() that
    # bypasses the shared cache would push the count to 2 instead of silently reusing it.
    assert provider.get_files() == ["a.py"]

    diff_files = provider.get_diff_files()

    assert [f.filename for f in diff_files] == ["a.py"]
    assert provider.get_files() == ["a.py"]
    assert client.get_pull_requests_changes.call_count == 1


MEMOIZED_PROVIDERS = {
    AzureDevopsProvider.__name__,
    BitbucketProvider.__name__,
    BitbucketServerProvider.__name__,
    CodeCommitProvider.__name__,
    GerritProvider.__name__,
    GiteaProvider.__name__,
    GithubProvider.__name__,
    LocalGitProvider.__name__,
}

# Providers whose get_languages() is cheap or was already memoized, so this change
# deliberately leaves them alone:
# - GitLabProvider memoized before this change (see gitlab_provider.get_languages).
# - PlainDiffGitProvider derives it from an in-memory diff string.
# - DiffInputProvider (mosaico) returns a value stored in __init__.
NOT_MEMOIZED_BY_DESIGN = {"GitLabProvider", "PlainDiffGitProvider", "DiffInputProvider"}


def _shipped_providers():
    """Return every GitProvider subclass defined under pr_agent/, keyed by class name.

    Discovered by module rather than listed by hand so a provider added later is covered
    without editing this file. Classes defined in tests are skipped because the suite
    registers its own throwaway GitProvider subclasses.
    """
    import pkgutil
    from importlib import import_module

    import pr_agent.git_providers as git_providers
    from pr_agent.git_providers.git_provider import GitProvider

    modules = ["pr_agent.git_providers"]
    for info in pkgutil.iter_modules(git_providers.__path__):
        modules.append(f"pr_agent.git_providers.{info.name}")
    modules.append("pr_agent.mosaico.diff_provider")

    found = {}
    for name in modules:
        # A provider module that cannot be imported is a real failure; swallowing it here
        # would let a newly added provider slip past the guard below.
        module = import_module(name)
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, GitProvider)
                and value is not GitProvider
                and value.__module__.startswith("pr_agent")
            ):
                found[value.__name__] = value
    return found


def test_every_provider_get_languages_is_memoized_or_exempt():
    """Guard the sweep: a new provider must either memoize or be listed as exempt."""
    shipped = _shipped_providers()

    overriding = {name for name, cls in shipped.items() if "get_languages" in cls.__dict__}
    unaccounted = overriding - MEMOIZED_PROVIDERS - NOT_MEMOIZED_BY_DESIGN
    assert not unaccounted, f"get_languages() overridden without a memo or exemption: {sorted(unaccounted)}"

    # Every memoized provider must really override get_languages(), so renaming one in
    # MEMOIZED_PROVIDERS cannot quietly stop testing anything.
    assert MEMOIZED_PROVIDERS <= overriding
    assert not (MEMOIZED_PROVIDERS & NOT_MEMOIZED_BY_DESIGN)
    assert NOT_MEMOIZED_BY_DESIGN <= set(shipped)


def test_bitbucket_construction_preserves_request_scoped_file_list():
    shared_files = ["shared.py"]
    settings = SimpleNamespace(get=lambda key, default=None: {
        "BITBUCKET.AUTH_TYPE": "bearer",
        "BITBUCKET.BEARER_TOKEN": "test-token",
    }.get(key, default))
    pr = MagicMock(**{
        "_BitbucketBase__data": {
            "links": {
                "comments": {"href": "https://api.bitbucket.org/comments"},
                "self": {"href": "https://api.bitbucket.org/pullrequests/1"},
            }
        }
    })
    with (
        request_cycle_context({"git_files": shared_files}),
        patch("pr_agent.git_providers.bitbucket_provider.get_settings", return_value=settings),
        patch("pr_agent.git_providers.bitbucket_provider.Cloud") as cloud,
    ):
        cloud.return_value.workspaces.get.return_value.repositories.get.return_value.pullrequests.get.return_value = pr
        provider = BitbucketProvider("https://bitbucket.org/workspace/repo/pull-requests/1")

        assert context["git_files"] is shared_files
        assert provider.get_files() is shared_files
        pr.diffstat.assert_not_called()
