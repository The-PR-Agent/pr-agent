"""Verify the shared line-link contract across git providers."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from pr_agent.git_providers import _GIT_PROVIDERS
from pr_agent.git_providers.azuredevops_provider import AzureDevopsProvider
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider
from pr_agent.git_providers.bitbucket_server_provider import BitbucketServerProvider
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.gitea_provider import GiteaProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider


class AnchorTier(Enum):
    RANGE = "range"
    SINGLE_LINE = "single-line"
    FILE_ONLY = "file-only"


@dataclass(frozen=True)
class LinkCase:
    name: str
    start: int
    end: int | str | None


@dataclass(frozen=True)
class ProviderContract:
    name: str
    tier: AnchorTier
    factory: Callable[[], GitProvider]
    extract_anchor: Callable[[str], str]
    file_reference: str
    expected_anchors: tuple[str, str, str, str, str]


FILE = "src/app.py"
BRANCH = "feature/test"
COMMON_INPUTS = (
    LinkCase("file", -1, None),
    LinkCase("single-line", 7, None),
    LinkCase("ordered-range", 4, 10),
    LinkCase("inverted-range", 10, 4),
    LinkCase("malformed-end", 10, "not-a-number"),
)


def _github_provider() -> GithubProvider:
    provider = GithubProvider.__new__(GithubProvider)
    provider.base_url_html = "https://github.example"
    provider.repo = "owner/repo"
    provider.pr_num = 7
    return provider


def _gitlab_provider() -> GitLabProvider:
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.gl = SimpleNamespace(url="https://gitlab.example")
    provider.id_project = "owner/repo"
    provider.mr = SimpleNamespace(
        web_url="https://gitlab.example/owner/repo/-/merge_requests/7",
        source_branch=BRANCH,
    )
    return provider


def _gitea_provider() -> GiteaProvider:
    provider = GiteaProvider.__new__(GiteaProvider)
    provider.base_url_html = "https://gitea.example"
    provider.owner = "owner"
    provider.repo = "repo"
    provider.logger = MagicMock()
    provider.get_pr_branch = MagicMock(return_value=BRANCH)
    return provider


def _bitbucket_provider() -> BitbucketProvider:
    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.pr_url = "https://bitbucket.org/owner/repo/pull-requests/7"
    return provider


def _bitbucket_server_provider() -> BitbucketServerProvider:
    provider = BitbucketServerProvider.__new__(BitbucketServerProvider)
    provider.pr_url = "https://bitbucket.example/projects/OWNER/repos/repo/pull-requests/7"
    return provider


def _azure_devops_provider() -> AzureDevopsProvider:
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    provider.pr_url = "https://dev.azure.com/org/project/_git/repo/pullrequest/7"
    return provider


def _fragment(link: str) -> str:
    return unquote(urlsplit(link).fragment)


def _azure_file(link: str) -> str:
    return parse_qs(urlsplit(link).query)["path"][0]


_github_file_anchor = "diff-" + hashlib.sha256(FILE.encode("utf-8")).hexdigest()
_bitbucket_file_anchor = f"L{FILE}"
_bitbucket_server_file_anchor = FILE

PROVIDER_CONTRACTS = (
    ProviderContract(
        name="github",
        tier=AnchorTier.RANGE,
        factory=_github_provider,
        extract_anchor=_fragment,
        file_reference=_github_file_anchor,
        expected_anchors=(
            _github_file_anchor,
            f"{_github_file_anchor}R7",
            f"{_github_file_anchor}R4-R10",
            f"{_github_file_anchor}R10-R10",
            f"{_github_file_anchor}R10",
        ),
    ),
    ProviderContract(
        name="gitlab",
        tier=AnchorTier.RANGE,
        factory=_gitlab_provider,
        extract_anchor=_fragment,
        file_reference=FILE,
        expected_anchors=(
            "",
            "L7",
            "L4-10",
            "L10-10",
            "L10",
        ),
    ),
    ProviderContract(
        name="gitea",
        tier=AnchorTier.RANGE,
        factory=_gitea_provider,
        extract_anchor=_fragment,
        file_reference=FILE,
        expected_anchors=(
            "",
            "L7",
            "L4-L10",
            "L10-L10",
            "L10",
        ),
    ),
    ProviderContract(
        name="bitbucket-cloud",
        tier=AnchorTier.SINGLE_LINE,
        factory=_bitbucket_provider,
        extract_anchor=_fragment,
        file_reference=FILE,
        expected_anchors=(
            _bitbucket_file_anchor,
            f"{_bitbucket_file_anchor}T7",
            f"{_bitbucket_file_anchor}T4",
            f"{_bitbucket_file_anchor}T10",
            f"{_bitbucket_file_anchor}T10",
        ),
    ),
    ProviderContract(
        name="bitbucket-server",
        tier=AnchorTier.SINGLE_LINE,
        factory=_bitbucket_server_provider,
        extract_anchor=_fragment,
        file_reference=FILE,
        expected_anchors=(
            _bitbucket_server_file_anchor,
            f"{_bitbucket_server_file_anchor}?t=7",
            f"{_bitbucket_server_file_anchor}?t=4",
            f"{_bitbucket_server_file_anchor}?t=10",
            f"{_bitbucket_server_file_anchor}?t=10",
        ),
    ),
    ProviderContract(
        name="azure-devops",
        tier=AnchorTier.FILE_ONLY,
        factory=_azure_devops_provider,
        extract_anchor=_azure_file,
        file_reference=FILE,
        expected_anchors=(FILE,) * len(COMMON_INPUTS),
    ),
)


def _contract_case_params():
    for contract in PROVIDER_CONTRACTS:
        for case, expected_anchor in zip(COMMON_INPUTS, contract.expected_anchors, strict=True):
            yield pytest.param(
                contract,
                case,
                expected_anchor,
                id=f"{contract.name}-{contract.tier.value}-{case.name}",
            )


CONTRACT_CASES = tuple(_contract_case_params())


def test_every_registered_line_link_implementation_has_a_contract():
    implementations = {
        provider_type
        for provider_type in _GIT_PROVIDERS.values()
        if provider_type.get_line_link is not GitProvider.get_line_link
    }
    contracted = {type(contract.factory()) for contract in PROVIDER_CONTRACTS}

    assert implementations == contracted


@pytest.mark.parametrize("contract", PROVIDER_CONTRACTS, ids=lambda contract: contract.name)
def test_contract_tier_matches_expected_anchor_shapes(contract: ProviderContract):
    _, single_line, ordered_range, inverted_range, malformed_end = contract.expected_anchors

    if contract.tier is AnchorTier.RANGE:
        assert inverted_range != malformed_end
    elif contract.tier is AnchorTier.SINGLE_LINE:
        assert len({single_line, ordered_range, inverted_range}) == 3
        assert inverted_range == malformed_end
    else:
        assert len(set(contract.expected_anchors)) == 1


@pytest.mark.parametrize(
    "contract",
    (contract for contract in PROVIDER_CONTRACTS if contract.tier is AnchorTier.FILE_ONLY),
    ids=lambda contract: contract.name,
)
def test_file_only_contract_ignores_all_line_arguments(contract: ProviderContract):
    links = {
        contract.factory().get_line_link(FILE, case.start, case.end)
        for case in COMMON_INPUTS
    }

    assert len(links) == 1


@pytest.mark.parametrize(
    "contract",
    (contract for contract in PROVIDER_CONTRACTS if contract.tier is AnchorTier.SINGLE_LINE),
    ids=lambda contract: contract.name,
)
def test_single_line_contract_ignores_end_argument(contract: ProviderContract):
    links = {
        contract.factory().get_line_link(FILE, 10, end)
        for end in (None, 12, 5, "not-a-number")
    }

    assert len(links) == 1


@pytest.mark.parametrize("contract,case,expected_anchor", CONTRACT_CASES)
def test_get_line_link_follows_provider_contract(
    contract: ProviderContract,
    case: LinkCase,
    expected_anchor: str,
):
    provider = contract.factory()
    link = provider.get_line_link(FILE, case.start, case.end)

    if not link:
        pytest.fail("get_line_link returned an empty URL", pytrace=False)
    assert contract.file_reference in unquote(link)
    assert contract.extract_anchor(link) == expected_anchor
