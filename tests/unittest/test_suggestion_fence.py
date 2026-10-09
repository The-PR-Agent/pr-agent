"""A suggestion whose code contains a ``` line is fenced with more backticks.

Every place that later finds that block again -- the diff fallbacks of the
Bitbucket, Bitbucket Server, Azure DevOps and GitHub providers, GitHub's
invalid-comment repair, Gerrit's description/code split, and the inline
comment dedup -- has to read the longer fence as one block too.
"""

import pytest

from pr_agent.algo.inline_comment_dedup import extract_suggestion_code
from pr_agent.algo.utils import get_suggestion_fence
from pr_agent.git_providers.azuredevops_provider import AzureDevopsProvider
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider
from pr_agent.git_providers.bitbucket_server_provider import BitbucketServerProvider
from pr_agent.git_providers.gerrit_provider import GerritProvider
from pr_agent.git_providers.github_provider import GithubProvider

EXISTING = "Run it."
IMPROVED = "Run it:\n\n```bash\nmake run\n```"
HEADER = "**Suggestion:** Show the command [enhancement, importance: 7]"
BODY = f"{HEADER}\n````suggestion\n{IMPROVED}\n````"
ORIGINAL = {"existing_code": EXISTING, "improved_code": IMPROVED}


@pytest.mark.parametrize(
    ("code", "fence"),
    [
        ("return new()", "```"),
        ("", "```"),
        ("use `x` here", "```"),
        ("```bash\nmake run\n```", "````"),
        ("````md\n```py\nx\n```\n````", "`````"),
        ("~~~\nx\n~~~", "```"),
    ],
)
def test_suggestion_fence_is_longer_than_any_backtick_run(code, fence):
    assert get_suggestion_fence(code) == fence


def _assert_whole_block_replaced_by_diff(body):
    assert "suggestion" not in body
    assert body.startswith(HEADER)
    assert "```diff\n" in body
    assert "+```bash" in body and "+make run" in body
    # Nothing of the replaced block leaks out after the diff.
    assert body.rstrip().endswith("```")
    assert not body.rstrip().endswith("````")


@pytest.mark.parametrize("provider_cls", [BitbucketProvider, BitbucketServerProvider])
def test_bitbucket_diff_fallback_replaces_the_whole_block(provider_cls):
    provider = provider_cls.__new__(provider_cls)
    prepared = provider._prepare_code_suggestion({"body": BODY, "original_suggestion": ORIGINAL})
    _assert_whole_block_replaced_by_diff(prepared["body"])


def test_azure_diff_rendering_replaces_the_whole_block():
    _assert_whole_block_replaced_by_diff(AzureDevopsProvider._render_suggestion_as_diff(BODY, ORIGINAL))


def test_github_invalid_comment_repair_drops_the_whole_block():
    provider = GithubProvider.__new__(GithubProvider)
    comment = {"body": BODY + "\n\n<!-- marker -->", "path": "README.md", "line": 2, "start_line": 1}

    fixed = provider._try_fix_invalid_inline_comments([comment])

    assert fixed[0]["body"] == f"{HEADER}\n\n\n<!-- marker -->"


def test_gerrit_split_keeps_the_nested_fence_in_the_code():
    provider = GerritProvider.__new__(GerritProvider)

    description, code = provider.split_suggestion(BODY)

    assert code == IMPROVED + "\n"
    assert "make run" not in description


def test_dedup_reads_the_whole_suggestion_code():
    assert extract_suggestion_code(BODY) == IMPROVED


def test_three_backtick_blocks_are_read_as_before():
    body = f"{HEADER}\n```suggestion\nreturn new()\n```"
    assert extract_suggestion_code(body) == "return new()"
    description, code = GerritProvider.__new__(GerritProvider).split_suggestion(body)
    assert code == "return new()\n"


def test_gerrit_split_keeps_a_literal_suggestion_example_in_the_code():
    improved = "Example:\n\n```suggestion\nnew()\n```\n\nEnd."
    body = f"{HEADER}\n````suggestion\n{improved}\n````"

    description, code = GerritProvider.__new__(GerritProvider).split_suggestion(body)

    assert code == improved + "\n"
