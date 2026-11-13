import copy
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github import Auth, Github
from jinja2 import Environment, meta

from pr_agent.algo.ticket_comments import normalize_ticket_comments, ticket_comment_limits
from pr_agent.algo.token_budget import AttemptTokenBudget
from pr_agent.commands.ticket_pr_compliance_check import extract_tickets, fit_related_tickets_to_prompt_budget
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.azuredevops_provider import AzureDevopsProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.plain_diff_provider import PlainDiffGitProvider
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


@pytest.fixture(autouse=True)
def comment_settings():
    keys = ["config.ticket_comments_max", "config.ticket_comments_max_characters", "config.max_model_tokens"]
    before = snapshot_settings(keys)
    get_settings().set(keys[0], 2)
    get_settings().set(keys[1], 2000)
    yield
    restore_settings(before)


def azure_provider(comments):
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    provider.workspace_slug = "project"
    provider.repo_slug = "repo"
    provider.pr_num = 1
    provider.azure_devops_client = MagicMock()
    provider.azure_devops_client.get_pull_request_work_item_refs.return_value = [SimpleNamespace(id="7")]
    provider.azure_devops_board_client = MagicMock()
    provider.azure_devops_board_client.get_work_items.return_value = [SimpleNamespace(
        id=7, url="https://dev.azure.com/org/project/_apis/wit/workItems/7",
        fields={"System.Title": "Ticket", "System.Description": "Description"},
    )]
    provider.azure_devops_board_client.get_comments.return_value = SimpleNamespace(comments=comments)
    return provider


def comment(text, **kwargs):
    return SimpleNamespace(text=text, created_date="2026-11-13T00:00:00Z", **kwargs)


@pytest.mark.parametrize("maximum", [0, -1, "invalid", [], True])
def test_disabled_or_invalid_setting_does_not_fetch_comments(maximum):
    get_settings().set("config.ticket_comments_max", maximum)
    provider = azure_provider([comment("new requirements")])
    assert "comments" not in provider.get_linked_work_items()[0]
    provider.azure_devops_board_client.get_comments.assert_not_called()


def test_azure_comments_are_bounded_and_exclude_deleted_and_system_entries():
    provider = azure_provider([
        comment("deleted", is_deleted=True),
        comment("system", comment_type="system"),
        comment('<p>Use ISO dates <img src="https://private.test/screenshot.png?token=secret"></p>'),
        comment("x" * 2100),
        comment("too old"),
    ])
    comments = provider.get_linked_work_items()[0]["comments"]
    assert len(comments) == 2
    assert comments[0]["body"] == "Use ISO dates [image: screenshot.png]"
    assert comments[1]["body"] == "x" * 2000
    provider.azure_devops_board_client.get_comments.assert_called_once_with(
        project="project", work_item_id=7, top=2, order="desc", include_deleted=False,
    )


async def test_azure_extraction_passes_comments_without_another_api_call():
    provider = azure_provider([comment("Use commas")])
    provider.get_user_description = lambda: ""
    provider.get_pr_branch = lambda: ""
    provider.get_pr_title = lambda: ""
    tickets = await extract_tickets(provider)
    assert tickets[0]["comments"][0]["body"] == "Use commas"
    assert provider.azure_devops_board_client.get_comments.call_count == 1


def test_comment_failure_preserves_ticket_description():
    provider = azure_provider([])
    provider.azure_devops_board_client.get_comments.side_effect = RuntimeError("permission denied")
    ticket = provider.get_linked_work_items()[0]
    assert ticket["body"] == "Description"
    assert not ticket.get("comments")


async def test_unsupported_provider_keeps_existing_behavior():
    provider = PlainDiffGitProvider.__new__(PlainDiffGitProvider)
    assert not provider.supports_ticket_comments()
    assert provider.get_ticket_comments("ignored", 2) == []
    assert await extract_tickets(provider) is None


@pytest.mark.parametrize("body", [
    "![screenshot](https://private.test/image.png)",
    "![screenshot [detail]](https://private.test/image_(1).png)",
    "[![screenshot](https://private.test/thumb.png)](https://private.test/image.png)",
    "![screenshot][ref]\n\n[ref]: https://private.test/image.png",
    "![screenshot][ref]\n\n[ref]:\n  https://private.test/image.png",
    '<a href="https://private.test/image.png"><img alt="screenshot" src="https://private.test/thumb.png"></a>',
])
def test_image_urls_are_removed_before_character_truncation(body):
    normalized = normalize_ticket_comments([{"body": body}], 1, 2000)[0]["body"]
    assert normalized.startswith("[image: screenshot")
    assert "https://" not in normalized


def test_comment_limits_are_bounded_and_timestamps_are_normalized():
    get_settings().set("config.ticket_comments_max", "1000")
    get_settings().set("config.ticket_comments_max_characters", 1000000)
    assert ticket_comment_limits() == (100, 10000)
    normalized = normalize_ticket_comments([
        {"body": "text", "created_at": "2026-11-13T08:00:00+08:00"},
    ], 1, 10)
    assert normalized[0]["created_at"] == "2026-11-13T00:00:00+00:00"


def test_comment_normalization_does_not_require_bitbucket_dependencies():
    script = """
import sys
sys.modules['bs4'] = None
sys.modules['atlassian'] = None
from pr_agent.algo.ticket_comments import normalize_ticket_comments
assert normalize_ticket_comments([{'body': '<p>Use ISO dates</p>'}], 1, 2000)[0]['body'] == 'Use ISO dates'
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_github_fetches_newest_comments_in_one_call_and_excludes_own_identity(monkeypatch):
    provider = GithubProvider.__new__(GithubProvider)
    provider.github_client = Github(auth=Auth.Token("test-token"))
    request = MagicMock(return_value=(200, {}, json.dumps({"data": {
        "viewer": {"login": "pr-agent[bot]"},
        "repository": {"issue": {"comments": {"nodes": [
            {"body": "old", "createdAt": "2026-11-12T00:00:00Z", "author": {"login": "human"}},
            {"body": "own output", "createdAt": "2026-11-13T00:00:00Z", "author": {"login": "PR-Agent[bot]"}},
            {"body": "![diagram](https://private.test/img.png)", "createdAt": "2026-11-13T01:00:00Z",
             "author": {"login": "human"}},
        ]}}},
    }})))
    monkeypatch.setattr(provider.github_client._Github__requester, "requestJson", request)
    comments = provider.get_ticket_comments("https://github.com/owner/repo/issues/7", 2)
    assert [c["body"] for c in comments] == ["[image: diagram]", "old"]
    request.assert_called_once()
    payload = request.call_args.kwargs["input"]
    assert payload["variables"] == {"owner": "owner", "repo": "repo", "number": 7, "count": 2}


def test_budget_removes_oldest_comments_before_ticket_bodies():
    get_settings().set("config.max_model_tokens", 550)
    raw = {"related_tickets": [{
        "title": "ticket", "body": "Keep description", "comments": [
            {"body": "new requirement", "created_at": "2026-11-13T00:00:00Z"},
            {"body": "old context " * 1000, "created_at": "2026-11-12T00:00:00Z"},
        ],
    }]}
    before = copy.deepcopy(raw)
    fitted, _ = fit_related_tickets_to_prompt_budget(
        None, raw, "", "{{ related_tickets | tojson }}", "gpt-4o-mini",
        default_output_tokens=0, additional_input_reserve=0,
    )
    assert len(fitted["related_tickets"]) == 1
    assert [c["body"] for c in fitted["related_tickets"][0]["comments"]] == ["new requirement"]
    assert raw == before


def test_budget_trims_flattened_sub_issue_comments_with_real_review_prompt():
    template = get_settings().get("pr_review_prompt").user
    environment = Environment(autoescape=True)
    variables = {name: "" for name in meta.find_undeclared_variables(environment.parse(template))}
    child = {
        "ticket_url": "child", "title": "Child", "body": "Keep child requirements",
        "comments": [{"body": "old discussion " * 1000, "created_at": "2026-11-12T00:00:00+00:00"}],
    }
    parent = {
        "ticket_url": "parent", "title": "Parent", "body": "Keep parent requirements",
        "comments": [{"body": "new requirement", "created_at": "2026-11-13T00:00:00+00:00"}],
        "sub_issues": [child],
    }
    # extract_and_cache_pr_tickets keeps the nested records and appends shallow copies for rendering.
    variables["related_tickets"] = [parent, {**child, "parent_ticket_url": "parent"}]
    before = copy.deepcopy(variables)
    minimal = copy.deepcopy(variables)
    minimal["related_tickets"][1]["comments"] = []
    budget = AttemptTokenBudget.for_prompt_attempt("gpt-4o-mini", None, minimal, "", template, ai_handler=None)
    get_settings().set("config.max_model_tokens", budget.prompt_tokens + 50)

    fitted, _ = fit_related_tickets_to_prompt_budget(
        None, variables, "", template, "gpt-4o-mini", default_output_tokens=0, additional_input_reserve=0,
    )
    assert len(fitted["related_tickets"]) == 2
    assert fitted["related_tickets"][1]["comments"] == []
    rendered = environment.from_string(template).render(**fitted)
    assert "Keep child requirements" in rendered
    assert "Keep parent requirements" in rendered
    assert "new requirement" in rendered
    assert "old discussion" not in rendered
    assert variables == before


@pytest.mark.parametrize("prompt_name", [
    "pr_review_prompt", "pr_description_prompt", "pr_description_only_files_prompts",
    "pr_description_only_description_prompts", "pr_questions_prompt",
])
def test_prompts_mark_comments_as_untrusted_after_description(prompt_name):
    template = Environment(autoescape=True).from_string(get_settings().get(prompt_name).user)
    ticket = {"ticket_url": "url", "title": "title", "body": "original description",
              "comments": [{"body": "changed requirement"}]}
    rendered = template.render(related_tickets=[ticket])
    assert "untrusted user content" in rendered
    assert rendered.index("original description") < rendered.index("changed requirement")
    ticket.pop("comments")
    assert "Ticket Comments" not in template.render(related_tickets=[ticket])
