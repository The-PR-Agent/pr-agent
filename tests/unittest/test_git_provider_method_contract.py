"""Verify the shared method contract across git providers.

One row per method: every implementation must accept the base signature and declare the base
return type, and must behave as its tier says. Tiers exist because providers legitimately
differ: a backend either supports the operation, has nothing to do, or declares it unsupported.
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
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
    check_execution: bool = True


@dataclass(frozen=True)
class DeliberateMismatch:
    reason: str


@dataclass(frozen=True)
class PredicateContract:
    name: str
    evidence: tuple[str, ...]
    deliberate_mismatches: dict[str, DeliberateMismatch]


@dataclass(frozen=True)
class SuggestionOutcomeContract:
    provider_name: str
    build_provider: Callable[..., GitProvider]
    make_fail: Callable[..., None]
    make_succeed: Callable[..., None]
    payload: list[dict]
    deliberate_mismatch: DeliberateMismatch | None = None


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


def _bitbucket_server(monkeypatch) -> BitbucketServerProvider:
    provider = BitbucketServerProvider.__new__(BitbucketServerProvider)
    provider.workspace_slug = "PRJ"
    provider.repo_slug = "repo"
    provider.pr_num = 7
    provider.bitbucket_client = MagicMock()
    provider.bitbucket_client.get_pull_requests_activities.return_value = [{
        "action": "COMMENTED",
        "comment": {"id": COMMENT_ID, "version": 1, "text": COMMENT_BODY},
    }]
    return provider


def _bitbucket(monkeypatch) -> BitbucketProvider:
    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.pr = MagicMock()

    comment = MagicMock()
    comment.raw = COMMENT_BODY
    provider.pr.comments.return_value = [comment]

    return provider


def _codecommit(monkeypatch) -> CodeCommitProvider:
    provider = CodeCommitProvider.__new__(CodeCommitProvider)
    provider.repo_name = "repo"
    provider.pr_num = 7
    provider.pr_url = "https://us-east-1.console.aws.amazon.com/codesuite/codecommit/repositories/repo/pull-requests/7"
    provider.pr = SimpleNamespace(
        source_commit="source",
        destination_commit="destination",
        targets=[
            SimpleNamespace(
                repository_name="repo",
                source_commit="source",
                destination_commit="destination",
            )
        ],
    )
    provider.codecommit_client = MagicMock()
    provider.codecommit_client.get_comments_for_pull_request.return_value = [
        {
            "repositoryName": "repo",
            "beforeCommitId": "destination",
            "afterCommitId": "source",
            "comments": [{
                "commentId": "comment-1",
                "content": COMMENT_BODY,
                "creationDate": datetime(2024, 1, 1, tzinfo=timezone.utc),
            }],
        }
    ]
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
    "bitbucket": (BitbucketProvider, _bitbucket),
    "bitbucket-server": (BitbucketServerProvider, _bitbucket_server),
    "codecommit": (CodeCommitProvider, _codecommit),
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

PREDICATE_CONTRACTS = (
    PredicateContract(
        name="supports_review_comment_identity",
        evidence=("edit_comment",),
        deliberate_mismatches={
            "gitea": DeliberateMismatch(
                "Gitea forwards identity arguments but cannot safely activate identity tracking "
                "until it normalizes dictionary-shaped comment payloads."
            ),
        },
    ),
    PredicateContract(
        name="supports_thread_resolution",
        evidence=("resolve_comment_thread",),
        deliberate_mismatches={
            "gitlab": DeliberateMismatch(
                "GitLab resolves note IDs while /ask_line addresses discussion IDs, so thread resolution "
                "must remain disabled."
            ),
        },
    ),
)


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
            supported=(
                "github",
                "gitlab",
                "gitea",
                "gerrit",
                "azure-devops",
                "bitbucket-server",
                "bitbucket",
                "codecommit",
            ),
            not_implemented=("local",),
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
    MethodContract(
        name="publish_inline_comment",
        args=(),
        noop_value=None,
        check_supported=lambda _: None,
        tiers=_tiers(),
        # Signature-only contract: catches signature drift against GitProvider without
        # requiring live backends or mock state for every provider.
        check_return_annotation=False,
        check_execution=False,
    ),
    MethodContract(
        name="get_repo_context_ref",
        args=(),
        noop_value=None,
        check_supported=lambda _: None,
        tiers=_tiers(
            supported=("github", "gitlab", "gitea", "azure-devops", "bitbucket", "bitbucket-server"),
        ),
        # Signature + return-annotation contract: the providers that fetch repo-context files
        # must expose the same hook so the cache can key on the revision being read.
        check_execution=False,
    ),
)


SUGGESTION_PAYLOAD = [{
    "body": "description\n```suggestion\nnew\n```",
    "relevant_file": "app.py",
    "relevant_lines_start": 1,
    "relevant_lines_end": 1,
}]


def _build_github_suggestion_provider(monkeypatch, tmp_path) -> GithubProvider:
    provider = _github(monkeypatch)
    provider.validate_comments_inside_hunks = lambda suggestions: suggestions
    return provider


def _build_gitlab_suggestion_provider(monkeypatch, tmp_path) -> GitLabProvider:
    provider = _gitlab(monkeypatch)
    provider.resolve_outdated_inline_threads = MagicMock()
    provider.get_diff_files = MagicMock(return_value=[SimpleNamespace(filename="app.py", head_file="orig\n")])
    return provider


def _build_gerrit_suggestion_provider(monkeypatch, tmp_path) -> GerritProvider:
    provider = _gerrit(monkeypatch)
    provider.repo_path = str(tmp_path)
    (tmp_path / "app.py").write_text("orig\n")
    monkeypatch.setattr("pr_agent.git_providers.gerrit_provider.upload_patch", lambda *_: "https://patch.example/1")
    monkeypatch.setattr("pr_agent.git_providers.gerrit_provider.diff", lambda *_, **__: "patch")
    monkeypatch.setattr("pr_agent.git_providers.gerrit_provider.reset_local_changes", lambda *_: None)
    return provider


def _build_azure_devops_suggestion_provider(monkeypatch, tmp_path) -> AzureDevopsProvider:
    provider = _azure_devops(monkeypatch)
    provider.workspace_slug = "project"
    provider.repo_slug = "repo"
    provider.pr_num = 7
    provider.azure_devops_client = MagicMock()
    provider._resolve_diff_file_path = MagicMock(return_value="/app.py")
    provider._get_suggestion_end_offset = MagicMock(return_value=1)
    return provider


def _build_codecommit_suggestion_provider(monkeypatch, tmp_path) -> CodeCommitProvider:
    provider = _codecommit(monkeypatch)
    provider.pr_num = 123
    provider.codecommit_client = MagicMock()
    return provider


def _succeed_codecommit_suggestions(provider: CodeCommitProvider, monkeypatch, tmp_path):
    provider._get_target_contexts_for_file = MagicMock(return_value=[{
        "repository_name": "repo",
        "destination_commit": "dest",
        "source_commit": "src",
    }])


def _build_local_suggestion_provider(monkeypatch, tmp_path) -> LocalGitProvider:
    provider = _bare(LocalGitProvider)(monkeypatch)
    provider.improve_path = str(tmp_path / "improve.md")
    return provider


def _build_plain_diff_suggestion_provider(monkeypatch, tmp_path) -> PlainDiffGitProvider:
    provider = _bare(PlainDiffGitProvider)(monkeypatch)
    provider.output_path = None
    return provider


SUGGESTION_OUTCOME_CONTRACTS = (
    SuggestionOutcomeContract(
        provider_name="github",
        build_provider=_build_github_suggestion_provider,
        make_fail=lambda p, mp, tmp: setattr(p, "publish_inline_comments", MagicMock(return_value=False)),
        make_succeed=lambda p, mp, tmp: setattr(p, "publish_inline_comments", MagicMock(return_value=True)),
        payload=SUGGESTION_PAYLOAD,
    ),
    SuggestionOutcomeContract(
        provider_name="gitlab",
        build_provider=_build_gitlab_suggestion_provider,
        make_fail=lambda p, mp, tmp: setattr(
            p, "send_inline_comment", MagicMock(side_effect=RuntimeError("network down"))
        ),
        make_succeed=lambda p, mp, tmp: setattr(p, "send_inline_comment", MagicMock(return_value=True)),
        payload=SUGGESTION_PAYLOAD,
        deliberate_mismatch=DeliberateMismatch(
            "GitLab unconditionally returns True; issue #3129 owns reporting total failures."
        ),
    ),
    SuggestionOutcomeContract(
        provider_name="gitea",
        build_provider=lambda mp, tmp: _gitea(mp),
        make_fail=lambda p, mp, tmp: setattr(p, "publish_inline_comments", MagicMock(return_value=False)),
        make_succeed=lambda p, mp, tmp: setattr(p, "publish_inline_comments", MagicMock(return_value=True)),
        payload=SUGGESTION_PAYLOAD,
    ),
    SuggestionOutcomeContract(
        provider_name="gerrit",
        build_provider=_build_gerrit_suggestion_provider,
        make_fail=lambda p, mp, tmp: mp.setattr(
            "pr_agent.git_providers.gerrit_provider.add_comment",
            MagicMock(side_effect=RuntimeError("network down")),
        ),
        make_succeed=lambda p, mp, tmp: mp.setattr(
            "pr_agent.git_providers.gerrit_provider.add_comment", lambda *_: None
        ),
        payload=SUGGESTION_PAYLOAD,
    ),
    SuggestionOutcomeContract(
        provider_name="azure-devops",
        build_provider=_build_azure_devops_suggestion_provider,
        make_fail=lambda p, mp, tmp: setattr(
            p.azure_devops_client, "create_thread", MagicMock(side_effect=RuntimeError("network down"))
        ),
        make_succeed=lambda p, mp, tmp: setattr(p.azure_devops_client, "create_thread", MagicMock()),
        payload=SUGGESTION_PAYLOAD,
    ),
    SuggestionOutcomeContract(
        provider_name="bitbucket",
        build_provider=lambda mp, tmp: _bitbucket(mp),
        make_fail=lambda p, mp, tmp: setattr(p, "publish_inline_comments", MagicMock(return_value=False)),
        make_succeed=lambda p, mp, tmp: setattr(p, "publish_inline_comments", MagicMock(return_value=True)),
        payload=SUGGESTION_PAYLOAD,
    ),
    SuggestionOutcomeContract(
        provider_name="bitbucket-server",
        build_provider=lambda mp, tmp: _bitbucket_server(mp),
        make_fail=lambda p, mp, tmp: setattr(p, "publish_inline_comments", MagicMock(return_value=False)),
        make_succeed=lambda p, mp, tmp: setattr(p, "publish_inline_comments", MagicMock(return_value=True)),
        payload=SUGGESTION_PAYLOAD,
    ),
    SuggestionOutcomeContract(
        provider_name="codecommit",
        build_provider=_build_codecommit_suggestion_provider,
        make_fail=lambda p, mp, tmp: setattr(p, "_get_target_contexts_for_file", MagicMock(return_value=[])),
        make_succeed=_succeed_codecommit_suggestions,
        payload=SUGGESTION_PAYLOAD,
    ),
    SuggestionOutcomeContract(
        provider_name="local",
        build_provider=_build_local_suggestion_provider,
        make_fail=lambda p, mp, tmp: None,
        make_succeed=lambda p, mp, tmp: None,
        payload=SUGGESTION_PAYLOAD,
        deliberate_mismatch=DeliberateMismatch(
            "Local git provider writes suggestions to a local artifact file and unconditionally returns True."
        ),
    ),
    SuggestionOutcomeContract(
        provider_name="plain-diff",
        build_provider=_build_plain_diff_suggestion_provider,
        make_fail=lambda p, mp, tmp: None,
        make_succeed=lambda p, mp, tmp: None,
        payload=SUGGESTION_PAYLOAD,
        deliberate_mismatch=DeliberateMismatch(
            "Plain-diff provider renders suggestions to stdout or output file and unconditionally returns True."
        ),
    ),
    SuggestionOutcomeContract(
        provider_name="mosaico-diff",
        build_provider=lambda mp, tmp: _bare(DiffInputProvider)(mp),
        make_fail=lambda p, mp, tmp: None,
        make_succeed=lambda p, mp, tmp: None,
        payload=SUGGESTION_PAYLOAD,
        deliberate_mismatch=DeliberateMismatch("Mosaico diff is a no-op provider that unconditionally returns True."),
    ),
)


def _rows(tier: Tier | None = None, check_execution_only: bool = False):
    for contract in METHOD_CONTRACTS:
        if check_execution_only and not contract.check_execution:
            continue
        for provider_name, provider_tier in contract.tiers.items():
            if tier is None or provider_tier is tier:
                yield pytest.param(provider_name, contract, id=f"{provider_name}-{contract.name}")


def _predicate_rows():
    for contract in PREDICATE_CONTRACTS:
        for provider_name in PROVIDERS:
            yield pytest.param(provider_name, contract, id=f"{provider_name}-{contract.name}")


def _has_evidence(provider_type: type[GitProvider], contract: PredicateContract) -> bool:
    for name in contract.evidence:
        member = getattr(provider_type, name, None)
        if member is not getattr(GitProvider, name, None):
            return True
    return False


def test_every_registered_provider_has_a_contract_row():
    contracted = {provider_type for provider_type, _ in PROVIDERS.values()}

    assert set(_GIT_PROVIDERS.values()) <= contracted


@pytest.mark.parametrize("provider_name,contract", tuple(_predicate_rows()))
def test_predicate_truth_matches_evidence(provider_name: str, contract: PredicateContract):
    provider_type, _ = PROVIDERS[provider_name]
    mismatch = contract.deliberate_mismatches.get(provider_name)
    predicate = getattr(provider_type.__new__(provider_type), contract.name)

    if mismatch:
        assert mismatch.reason.strip()
        assert predicate() is False
    else:
        assert predicate() is _has_evidence(provider_type, contract)


@pytest.mark.parametrize("contract", PREDICATE_CONTRACTS, ids=lambda contract: contract.name)
def test_predicate_mismatches_name_only_providers_with_evidence(contract: PredicateContract):
    for provider_name in contract.deliberate_mismatches:
        provider_type, _ = PROVIDERS[provider_name]

        assert _has_evidence(provider_type, contract)


@pytest.mark.parametrize("contract", PREDICATE_CONTRACTS, ids=lambda contract: contract.name)
def test_predicate_contract_evidence_members_exist_on_registered_providers(contract: PredicateContract):
    assert contract.evidence
    for member_name in contract.evidence:
        assert any(hasattr(provider_type, member_name) for provider_type, _ in PROVIDERS.values())


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


@pytest.mark.parametrize("provider_name,contract", tuple(_rows(Tier.SUPPORTED, check_execution_only=True)))
def test_supported_tier_returns_the_contract_value(provider_name: str, contract: MethodContract, monkeypatch):
    _, factory = PROVIDERS[provider_name]
    provider = factory(monkeypatch)

    contract.check_supported(getattr(provider, contract.name)(*contract.args))


@pytest.mark.parametrize("provider_name,contract", tuple(_rows(Tier.NOOP, check_execution_only=True)))
def test_noop_tier_returns_the_empty_value_without_a_backend(provider_name: str, contract: MethodContract):
    provider_type, _ = PROVIDERS[provider_name]
    provider = provider_type.__new__(provider_type)

    result = getattr(provider, contract.name)(*contract.args)

    assert type(result) is type(contract.noop_value)
    assert result == contract.noop_value


@pytest.mark.parametrize("provider_name,contract", tuple(_rows(Tier.NOT_IMPLEMENTED, check_execution_only=True)))
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


@pytest.mark.parametrize(
    "provider_name",
    [
        name
        for name, (cls, _) in PROVIDERS.items()
        if "publish_code_suggestions" in cls.__dict__
    ],
)
def test_publish_code_suggestions_declares_bool_return(provider_name: str):
    provider_type, _ = PROVIDERS[provider_name]
    hints = get_type_hints(provider_type.publish_code_suggestions)
    assert hints.get("return") is bool


def test_every_provider_overriding_publish_code_suggestions_has_a_contract_row():
    overriding = {
        name
        for name, (cls, _) in PROVIDERS.items()
        if "publish_code_suggestions" in cls.__dict__
    }
    contracted = {contract.provider_name for contract in SUGGESTION_OUTCOME_CONTRACTS}
    assert len(contracted) == len(SUGGESTION_OUTCOME_CONTRACTS)
    assert contracted == overriding


@pytest.mark.parametrize(
    "contract",
    [
        pytest.param(contract, id=f"{contract.provider_name}-publish_code_suggestions-failure")
        for contract in SUGGESTION_OUTCOME_CONTRACTS
    ],
)
def test_publish_code_suggestions_returns_false_on_total_failure(
    contract: SuggestionOutcomeContract, monkeypatch, tmp_path
):
    provider = contract.build_provider(monkeypatch, tmp_path)
    contract.make_fail(provider, monkeypatch, tmp_path)
    if contract.deliberate_mismatch is not None:
        assert contract.deliberate_mismatch.reason.strip()
        assert provider.publish_code_suggestions(contract.payload) is True
    else:
        assert provider.publish_code_suggestions(contract.payload) is False


@pytest.mark.parametrize(
    "contract",
    [
        pytest.param(contract, id=f"{contract.provider_name}-publish_code_suggestions-success")
        for contract in SUGGESTION_OUTCOME_CONTRACTS
    ],
)
def test_publish_code_suggestions_returns_true_on_success(
    contract: SuggestionOutcomeContract, monkeypatch, tmp_path
):
    provider = contract.build_provider(monkeypatch, tmp_path)
    contract.make_succeed(provider, monkeypatch, tmp_path)
    assert provider.publish_code_suggestions(contract.payload) is True
