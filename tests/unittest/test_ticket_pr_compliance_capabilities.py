from unittest.mock import MagicMock

import pytest

from pr_agent.git_providers import (
    AzureDevopsProvider,
    BitbucketProvider,
    BitbucketServerProvider,
    CodeCommitProvider,
    GerritProvider,
    GiteaProvider,
    GithubProvider,
    GitLabProvider,
    GitProvider,
    LocalGitProvider,
    PlainDiffGitProvider,
)
from pr_agent.tools.ticket_pr_compliance_check import _provider_supports, extract_tickets


class _BaseStubProvider(GitProvider):
    def is_supported(self, capability: str) -> bool:
        return False

    def get_files(self) -> list:
        return []

    def get_diff_files(self) -> list:
        return []

    def publish_description(self, pr_title: str, pr_body: str):
        pass

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        return False

    def get_languages(self):
        return {}

    def get_pr_branch(self):
        return ""

    def get_user_id(self):
        return ""

    def get_pr_description_full(self) -> str:
        return ""

    def get_repo_settings(self):
        return b""

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        pass

    def publish_inline_comment(
        self, body: str, relevant_file: str, relevant_line_in_file: str, original_suggestion=None
    ):
        pass

    def publish_inline_comments(self, comments: list[dict]):
        pass

    def remove_initial_comment(self):
        pass

    def remove_comment(self, comment):
        pass

    def get_issue_comments(self):
        return []

    def publish_labels(self, labels):
        pass

    def get_pr_labels(self, update=False):
        return []

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        return False

    def get_commit_messages(self) -> str:
        return ""


class _CustomGitHubSubstitute(_BaseStubProvider):
    def __init__(self, description="Fixes #101", branch="feature/102-branch"):
        self.repo = "owner/repo"
        self.base_url_html = "https://github.com"
        self._description = description
        self._branch = branch
        self.github_client = MagicMock()
        self.repo_obj = MagicMock()

        issue_101 = MagicMock(number=101, title="Issue 101", body="Body 101", labels=["bug"])
        issue_102 = MagicMock(number=102, title="Issue 102", body="Body 102", labels=["core"])
        self.repo_obj.get_issue.side_effect = lambda num: {101: issue_101, 102: issue_102}[num]

    def supports_issue_url_tickets(self) -> bool:
        return True

    def get_user_description(self) -> str:
        return self._description

    def get_pr_branch(self) -> str:
        return self._branch

    def _parse_issue_url(self, url: str):
        num = int(url.rsplit("/", 1)[-1])
        return self.repo, num

    def fetch_sub_issues(self, ticket_url: str) -> list:
        return []


class _CustomGitLabSubstitute(_BaseStubProvider):
    def __init__(self, description="Resolves group/project#55"):
        self.id_project = "group/project"
        self.gitlab_url = "https://gitlab.example.com"
        self._description = description
        self.gl = MagicMock()

        issue = MagicMock(
            iid=55,
            web_url="https://gitlab.example.com/group/project/-/issues/55",
            title="GitLab Issue 55",
            description="GitLab Description 55",
            labels=["backend", "p1"],
        )
        project = MagicMock()
        project.issues.get.return_value = issue
        self.gl.projects.get.return_value = project

    def supports_issue_reference_tickets(self) -> bool:
        return True

    def get_user_description(self) -> str:
        return self._description


class _CustomAzureSubstitute(_BaseStubProvider):
    def __init__(self, work_items=None):
        self._work_items = (
            work_items
            if work_items is not None
            else [
                {
                    "id": 999,
                    "url": "https://dev.azure.com/org/proj/_workitems/edit/999",
                    "title": "Work Item 999",
                    "body": "Work item body 999",
                    "acceptance_criteria": "AC 999",
                    "labels": ["azure-label"],
                }
            ]
        )

    def supports_linked_work_item_tickets(self) -> bool:
        return True

    def get_linked_work_items(self) -> list:
        return self._work_items


class _IndependentDuckTypedSubstitute:
    def __init__(self, capability: str):
        self._capability = capability
        self.repo = "duck/repo"
        self.base_url_html = "https://github.com"
        self.id_project = "duck/repo"
        self.gitlab_url = "https://gitlab.com"
        self.repo_obj = MagicMock()
        self.gl = MagicMock()

    def supports_issue_url_tickets(self) -> bool:
        return self._capability == "github"

    def supports_issue_reference_tickets(self) -> bool:
        return self._capability == "gitlab"

    def supports_linked_work_item_tickets(self) -> bool:
        return self._capability == "azure"

    def get_user_description(self) -> str:
        if self._capability == "github":
            return "Fixes #42"
        if self._capability == "gitlab":
            return "Fixes duck/repo#42"
        return ""

    def get_pr_branch(self) -> str:
        return "main"

    def _parse_issue_url(self, url: str):
        return self.repo, 42

    def fetch_sub_issues(self, ticket_url: str) -> list:
        return []

    def get_linked_work_items(self) -> list:
        return [
            {
                "id": 42,
                "url": "https://dev.azure.com/duck/repo/_workitems/edit/42",
                "title": "Azure 42",
                "body": "Body 42",
                "acceptance_criteria": "",
                "labels": [],
            }
        ]


def test_base_git_provider_capabilities_default_false():
    provider = _BaseStubProvider()
    assert provider.supports_issue_url_tickets() is False
    assert provider.supports_issue_reference_tickets() is False
    assert provider.supports_linked_work_item_tickets() is False


@pytest.mark.parametrize(
    ("provider_cls", "expected_github", "expected_gitlab", "expected_azure"),
    [
        (GithubProvider, True, False, False),
        (GitLabProvider, False, True, False),
        (AzureDevopsProvider, False, False, True),
        (BitbucketProvider, False, False, False),
        (BitbucketServerProvider, False, False, False),
        (CodeCommitProvider, False, False, False),
        (LocalGitProvider, False, False, False),
        (GerritProvider, False, False, False),
        (GiteaProvider, False, False, False),
        (PlainDiffGitProvider, False, False, False),
    ],
)
def test_concrete_providers_declare_expected_ticket_capabilities(
    provider_cls, expected_github, expected_gitlab, expected_azure
):
    provider = provider_cls.__new__(provider_cls)
    assert provider.supports_issue_url_tickets() is expected_github
    assert provider.supports_issue_reference_tickets() is expected_gitlab
    assert provider.supports_linked_work_item_tickets() is expected_azure


@pytest.mark.asyncio
async def test_extract_tickets_routes_to_github_capability():
    provider = _CustomGitHubSubstitute()
    tickets = await extract_tickets(provider)

    assert tickets is not None
    assert len(tickets) == 2
    assert tickets[0]["ticket_id"] == 101
    assert tickets[0]["title"] == "Issue 101"
    assert tickets[1]["ticket_id"] == 102
    assert tickets[1]["title"] == "Issue 102"


@pytest.mark.asyncio
async def test_extract_tickets_routes_to_gitlab_capability():
    provider = _CustomGitLabSubstitute()
    tickets = await extract_tickets(provider)

    assert tickets is not None
    assert len(tickets) == 1
    assert tickets[0]["ticket_id"] == 55
    assert tickets[0]["title"] == "GitLab Issue 55"
    assert tickets[0]["body"] == "GitLab Description 55"
    assert tickets[0]["labels"] == "backend, p1"


@pytest.mark.asyncio
async def test_extract_tickets_routes_to_azure_capability():
    provider = _CustomAzureSubstitute()
    tickets = await extract_tickets(provider)

    assert tickets is not None
    assert len(tickets) == 1
    assert tickets[0]["ticket_id"] == 999
    assert tickets[0]["title"] == "Work Item 999"
    assert tickets[0]["body"] == "Work item body 999"
    assert tickets[0]["requirements"] == "AC 999"
    assert tickets[0]["labels"] == "azure-label"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["github", "gitlab", "azure"])
async def test_extract_tickets_supports_duck_typed_substitutes(route):
    provider = _IndependentDuckTypedSubstitute(route)
    if route == "github":
        provider.repo_obj.get_issue.return_value = MagicMock(number=42, title="GH 42", body="GH Body", labels=[])
    elif route == "gitlab":
        mock_proj = MagicMock()
        mock_proj.issues.get.return_value = MagicMock(
            iid=42, web_url="https://gitlab.com/duck/repo/-/issues/42", title="GL 42", description="GL Body", labels=[]
        )
        provider.gl.projects.get.return_value = mock_proj

    tickets = await extract_tickets(provider)
    assert tickets is not None
    assert len(tickets) == 1
    assert tickets[0]["ticket_id"] == 42


@pytest.mark.asyncio
async def test_extract_tickets_unsupported_provider_returns_none_without_asana():
    provider = _BaseStubProvider()
    provider.get_user_description = lambda: "No tickets here"
    result = await extract_tickets(provider)
    assert result is None


@pytest.mark.asyncio
async def test_extract_tickets_unsupported_provider_preserves_asana_results(monkeypatch):
    provider = _BaseStubProvider()
    provider.get_user_description = lambda: "Related: https://app.asana.com/0/99/123456789012"

    async def fake_fetch(urls, max_tickets, max_characters):
        assert urls == ["https://app.asana.com/0/99/123456789012"]
        return [{"ticket_id": "123456789012", "ticket_url": urls[0]}]

    monkeypatch.setattr("pr_agent.tools.ticket_pr_compliance_check._fetch_asana_ticket_contents", fake_fetch)
    tickets = await extract_tickets(provider)
    assert tickets == [{"ticket_id": "123456789012", "ticket_url": "https://app.asana.com/0/99/123456789012"}]


@pytest.mark.asyncio
async def test_extract_tickets_github_substitute_handles_issue_fetch_error():
    provider = _CustomGitHubSubstitute(description="Fixes #999", branch="")
    provider.repo_obj.get_issue.side_effect = RuntimeError("Issue not found")
    tickets = await extract_tickets(provider)
    assert tickets == []


@pytest.mark.asyncio
async def test_extract_tickets_gitlab_substitute_handles_project_error():
    provider = _CustomGitLabSubstitute()
    provider.gl.projects.get.side_effect = RuntimeError("GitLab API error")
    tickets = await extract_tickets(provider)
    assert tickets == []


@pytest.mark.asyncio
async def test_extract_tickets_azure_substitute_handles_item_error():
    provider = _CustomAzureSubstitute(work_items=[{"invalid": "data"}])
    tickets = await extract_tickets(provider)
    assert len(tickets) == 1
    assert tickets[0]["ticket_id"] is None


@pytest.mark.asyncio
async def test_extract_tickets_catches_top_level_exception():
    provider = _CustomGitHubSubstitute()
    provider.supports_issue_url_tickets = MagicMock(side_effect=RuntimeError("unexpected crash"))
    tickets = await extract_tickets(provider)
    assert tickets == []


def test_provider_supports_raises_for_unknown_capability():
    provider = _BaseStubProvider()
    with pytest.raises(AttributeError, match="unknown provider capability: 'supports_nonexistent_capability'"):
        _provider_supports(provider, "supports_nonexistent_capability")


def test_provider_supports_returns_false_for_missing_method_on_duck_typed_provider():
    duck_typed_provider = object()
    assert _provider_supports(duck_typed_provider, "supports_issue_url_tickets") is False


def test_provider_supports_returns_false_for_non_callable_attribute():
    class AttributeProvider:
        supports_issue_url_tickets = True

    assert _provider_supports(AttributeProvider(), "supports_issue_url_tickets") is False


def test_provider_supports_reads_callable_on_base_or_duck_typed_provider():
    class TruthyProvider:
        def supports_issue_url_tickets(self):
            return 1

    class FalsyProvider:
        def supports_issue_url_tickets(self):
            return 0

    assert _provider_supports(TruthyProvider(), "supports_issue_url_tickets") is True
    assert _provider_supports(FalsyProvider(), "supports_issue_url_tickets") is False
    assert _provider_supports(_BaseStubProvider(), "supports_issue_url_tickets") is False
