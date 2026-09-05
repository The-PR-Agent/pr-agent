"""Verify the shared method contract across git providers.

One row per method: every implementation must accept the base signature and declare the base
return type, and must behave as its tier says. Tiers exist because providers legitimately
differ: a backend either supports the operation, has nothing to do, or declares it unsupported.
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import MagicMock

import pytest

from pr_agent.git_providers import _GIT_PROVIDERS
from pr_agent.git_providers.azuredevops_provider import AzureDevopsProvider
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider
from pr_agent.git_providers.bitbucket_server_provider import BitbucketServerProvider
from pr_agent.git_providers.codecommit_provider import CodeCommitProvider
from pr_agent.git_providers.gerrit_provider import GerritProvider
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.gitea_provider import GiteaProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider
from pr_agent.git_providers.local_git_provider import LocalGitProvider
from pr_agent.git_providers.plain_diff_provider import PlainDiffGitProvider
from pr_agent.mosaico.diff_provider import DiffInputProvider


class Tier(Enum):
    SUPPORTED = "supported"  # talks to the backend and returns the contract's value
    NOOP = "no-op"  # returns the contract's empty value without touching any backend
    NOT_IMPLEMENTED = "not-implemented"  # raises NotImplementedError, so callers must guard


COMMENT_ID = 11
REACTION_ID = 5
COMMENT_BODY = "looks good"
COMMIT_MESSAGE = "fix: keep the contract"


@dataclass(frozen=True)
class MethodContract:
    name: str
    args: tuple
    noop_value: object
    check_supported: Callable[[object], None]
    tiers: dict[str, Tier]
    check_return_annotation: bool = True


def _github(monkeypatch) -> GithubProvider:
    provider = GithubProvider.__new__(GithubProvider)
    provider.base_url = "https://api.github.example"
    provider.repo = "owner/repo"
    provider.pr = MagicMock()
    provider.pr.get_issue_comments.return_value = [SimpleNamespace(body=COMMENT_BODY)]
    provider.pr.get_commits.return_value = [SimpleNamespace(commit=SimpleNamespace(message=COMMIT_MESSAGE))]
    provider.pr._requester.requestJsonAndCheck.return_value = ({}, {"id": REACTION_ID})
    return provider


def _gitlab(monkeypatch) -> GitLabProvider:
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "owner/repo"
    provider.id_mr = 7
    provider.mr = MagicMock()
    provider.mr.notes.list.return_value = [SimpleNamespace(body=COMMENT_BODY)]
    provider.mr.commits.return_value._list = [{"message": COMMIT_MESSAGE}]
    note = MagicMock()
    note.awardemojis.create.return_value = SimpleNamespace(id=REACTION_ID)
    note.awardemojis.list.return_value = [SimpleNamespace(name=REACTION_ID, delete=MagicMock())]
    provider.gl = MagicMock()
    provider.gl.projects.get.return_value.mergerequests.get.return_value.notes.get.return_value = note
    return provider


def _gitea(monkeypatch) -> GiteaProvider:
    provider = GiteaProvider.__new__(GiteaProvider)
    provider.logger = MagicMock()
    provider.owner = "owner"
    provider.repo = "repo"
    provider.pr_number = 7
    provider.enabled_pr = True
    provider.enabled_issue = False
    provider.issue_number = None
    provider.repo_api = MagicMock()
    provider.repo_api.list_all_comments.return_value = [SimpleNamespace(id=COMMENT_ID, body=COMMENT_BODY)]
    provider.repo_api.add_reaction_comment.return_value = SimpleNamespace(id=REACTION_ID)
    provider.repo_api.remove_reaction_comment.return_value = SimpleNamespace(status=200)
    provider.repo_api.get_pr_commits.return_value = [{"commit": {"message": COMMIT_MESSAGE}}]
    return provider


def _gerrit(monkeypatch) -> GerritProvider:
    provider = GerritProvider.__new__(GerritProvider)
    provider.parsed_url = SimpleNamespace()
    provider.refspec = "refs/changes/1"
    provider.repo = SimpleNamespace(head=SimpleNamespace(commit=SimpleNamespace(message=COMMIT_MESSAGE)))
    monkeypatch.setattr(
        "pr_agent.git_providers.gerrit_provider.list_comments", lambda *_: [{"message": COMMENT_BODY}]
    )
    return provider


def _azure_devops(monkeypatch) -> AzureDevopsProvider:
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    provider._get_threads = lambda: [SimpleNamespace(id=1, comments=[SimpleNamespace(content=COMMENT_BODY)])]
    return provider


def _bare(provider_type):
    """A provider with no backend wired at all: every call on it must succeed without one."""
    return lambda monkeypatch: provider_type.__new__(provider_type)


PROVIDERS: dict[str, tuple[type[GitProvider], Callable[[pytest.MonkeyPatch], GitProvider]]] = {
    "github": (GithubProvider, _github),
    "gitlab": (GitLabProvider, _gitlab),
    "gitea": (GiteaProvider, _gitea),
    "gerrit": (GerritProvider, _gerrit),
    "azure-devops": (AzureDevopsProvider, _azure_devops),
    "bitbucket": (BitbucketProvider, _bare(BitbucketProvider)),
    "bitbucket-server": (BitbucketServerProvider, _bare(BitbucketServerProvider)),
    "codecommit": (CodeCommitProvider, _bare(CodeCommitProvider)),
    "local": (LocalGitProvider, _bare(LocalGitProvider)),
    "plain-diff": (PlainDiffGitProvider, _bare(PlainDiffGitProvider)),
    "mosaico-diff": (DiffInputProvider, _bare(DiffInputProvider)),
}


def _tiers(supported=(), not_implemented=()) -> dict[str, Tier]:
    tiers = dict.fromkeys(PROVIDERS, Tier.NOOP)
    tiers.update(dict.fromkeys(supported, Tier.SUPPORTED))
    tiers.update(dict.fromkeys(not_implemented, Tier.NOT_IMPLEMENTED))
    return tiers


def _is_commit_text(value):
    assert isinstance(value, str)
    assert COMMIT_MESSAGE in value


def _is_comment_sequence(value):
    assert [comment.body for comment in list(value)] == [COMMENT_BODY]


def _is_reaction_id(value):
    assert type(value) is int
    assert value == REACTION_ID


def _is_success(value):
    assert value is True


REACTION_TIERS = _tiers(supported=("github", "gitlab", "gitea"), not_implemented=("gerrit",))

METHOD_CONTRACTS = (
    MethodContract(
        name="get_commit_messages",
        args=(),
        noop_value="",
        check_supported=_is_commit_text,
        tiers=_tiers(supported=("github", "gitlab", "gitea", "gerrit")),
    ),
    MethodContract(
        name="get_issue_comments",
        args=(),
        noop_value=[],
        check_supported=_is_comment_sequence,
        tiers=_tiers(
            supported=("github", "gitlab", "gitea", "gerrit", "azure-devops"),
            not_implemented=("bitbucket", "bitbucket-server", "codecommit", "local"),
        ),
        # Implementations narrow the base `Iterable` (a paginated list, a list of SDK objects),
        # so the return annotation is checked by behaviour rather than by equality.
        check_return_annotation=False,
    ),
    MethodContract(
        name="add_eyes_reaction",
        args=(COMMENT_ID,),
        noop_value=None,
        check_supported=_is_reaction_id,
        tiers=REACTION_TIERS,
    ),
    MethodContract(
        name="remove_reaction",
        args=(COMMENT_ID, REACTION_ID),
        noop_value=True,
        check_supported=_is_success,
        tiers=REACTION_TIERS,
    ),
)


def _rows(tier: Tier | None = None):
    for contract in METHOD_CONTRACTS:
        for provider_name, provider_tier in contract.tiers.items():
            if tier is None or provider_tier is tier:
                yield pytest.param(provider_name, contract, id=f"{provider_name}-{contract.name}")


def test_every_registered_provider_has_a_contract_row():
    contracted = {provider_type for provider_type, _ in PROVIDERS.values()}

    assert set(_GIT_PROVIDERS.values()) <= contracted


@pytest.mark.parametrize("contract", METHOD_CONTRACTS, ids=lambda contract: contract.name)
def test_every_contract_row_places_every_provider_in_a_tier(contract: MethodContract):
    assert set(contract.tiers) == set(PROVIDERS)


@pytest.mark.parametrize("provider_name,contract", tuple(_rows()))
def test_implementation_accepts_the_base_signature(provider_name: str, contract: MethodContract):
    provider_type, _ = PROVIDERS[provider_name]
    base = inspect.signature(getattr(GitProvider, contract.name))
    implementation = inspect.signature(getattr(provider_type, contract.name))

    def shape(signature):
        return [(parameter.name, parameter.kind, parameter.default) for parameter in signature.parameters.values()]

    assert shape(implementation) == shape(base)


@pytest.mark.parametrize(
    "provider_name,contract",
    tuple(row for row in _rows() if row.values[1].check_return_annotation),
)
def test_implementation_declares_the_base_return_type(provider_name: str, contract: MethodContract):
    provider_type, _ = PROVIDERS[provider_name]
    base_hints = get_type_hints(getattr(GitProvider, contract.name))
    implementation_hints = get_type_hints(getattr(provider_type, contract.name))

    assert implementation_hints.get("return") == base_hints["return"]


@pytest.mark.parametrize("provider_name,contract", tuple(_rows(Tier.SUPPORTED)))
def test_supported_tier_returns_the_contract_value(provider_name: str, contract: MethodContract, monkeypatch):
    _, factory = PROVIDERS[provider_name]
    provider = factory(monkeypatch)

    contract.check_supported(getattr(provider, contract.name)(*contract.args))


@pytest.mark.parametrize("provider_name,contract", tuple(_rows(Tier.NOOP)))
def test_noop_tier_returns_the_empty_value_without_a_backend(provider_name: str, contract: MethodContract):
    provider_type, _ = PROVIDERS[provider_name]
    provider = provider_type.__new__(provider_type)

    result = getattr(provider, contract.name)(*contract.args)

    assert type(result) is type(contract.noop_value)
    assert result == contract.noop_value


@pytest.mark.parametrize("provider_name,contract", tuple(_rows(Tier.NOT_IMPLEMENTED)))
def test_not_implemented_tier_raises(provider_name: str, contract: MethodContract):
    provider_type, _ = PROVIDERS[provider_name]
    provider = provider_type.__new__(provider_type)

    with pytest.raises(NotImplementedError):
        getattr(provider, contract.name)(*contract.args)


@pytest.mark.parametrize(
    "provider_name",
    [name for name, tier in REACTION_TIERS.items() if tier is not Tier.NOT_IMPLEMENTED],
)
def test_disable_eyes_short_circuits_before_any_backend_call(provider_name: str):
    provider_type, _ = PROVIDERS[provider_name]
    provider = provider_type.__new__(provider_type)

    assert provider.add_eyes_reaction(COMMENT_ID, disable_eyes=True) is None
