from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from gitlab.exceptions import GitlabError

from pr_agent.algo.git_patch_processing import RE_HUNK_HEADER
from pr_agent.algo.pr_processing import FallbackEligibleError
from pr_agent.commands.pr_add_docs import PRAddDocs
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider


@pytest.mark.parametrize("line", [True, False, 1.9, 0, -1])
def test_add_docs_rejects_invalid_model_line_anchors(line):
    command = object.__new__(PRAddDocs)
    command.prediction = yaml.safe_dump({"Code Documentation": [{
        "relevant file": "app.py", "relevant line": line,
        "doc placement": "after", "documentation": "Explain the function.",
    }]})
    with pytest.raises(FallbackEligibleError):
        command._prepare_pr_code_docs()


@pytest.mark.parametrize("placement", ["sideways", "", "above"])
def test_add_docs_does_not_reinterpret_invalid_placement_as_before(placement):
    command = object.__new__(PRAddDocs)
    command.prediction = yaml.safe_dump({"Code Documentation": [{
        "relevant file": "app.py", "relevant line": 1,
        "doc placement": placement, "documentation": "Explain the function.",
    }]})
    with pytest.raises(FallbackEligibleError):
        command._prepare_pr_code_docs()


def test_direct_doc_publication_does_not_coerce_fractional_anchors():
    command = object.__new__(PRAddDocs)
    command.git_provider = MagicMock()
    command.dedent_code = MagicMock(return_value="original\n# documentation")
    command.push_inline_docs({"Code Documentation": [{
        "relevant file": "app.py", "relevant line": 1.9,
        "doc placement": "after", "documentation": "Explain the function.",
    }]})
    command.dedent_code.assert_not_called()
    command.git_provider.publish_code_suggestions.assert_not_called()


@pytest.mark.parametrize("line", [1, "1"])
def test_add_docs_preserves_positive_integer_anchor_compatibility(line):
    command = object.__new__(PRAddDocs)
    command.prediction = yaml.safe_dump({"Code Documentation": [{
        "relevant file": "app.py", "relevant line": line,
        "doc placement": "before", "documentation": "Explain the function.",
    }]})
    assert command._prepare_pr_code_docs()["Code Documentation"][0]["relevant line"] == line


def _patch_file():
    return SimpleNamespace(
        filename="app.py",
        old_filename=None,
        head_file="def compute(x):\n    y = x + 1\n    return y\n",
        head_file_is_complete=True,
        patch="@@ -2,2 +2,2 @@\n-    y = x\n+    y = x + 1\n     return y\n",
    )


def test_push_inline_docs_converts_near_hunk_github_anchor_to_diff():
    command = object.__new__(PRAddDocs)
    provider = object.__new__(GithubProvider)
    provider.diff_files = [_patch_file()]
    provider.max_comment_chars = 65000
    published = []
    provider.publish_inline_comments = lambda comments: (published.extend(comments), True)[1]
    command.git_provider = provider

    assert command.push_inline_docs({"Code Documentation": [{
        "relevant file": "app.py",
        "relevant line": 1,
        "doc placement": "after",
        "documentation": '"""Compute result."""',
    }]}) is True

    assert len(published) == 1
    assert published[0]["line"] == 2
    assert "```suggestion" not in published[0]["body"]
    assert '```diff\n def compute(x):\n+    """Compute result."""\n```' in published[0]["body"]


def test_push_inline_docs_falls_back_to_gitlab_file_note_outside_diff():
    command = object.__new__(PRAddDocs)
    provider = object.__new__(GitLabProvider)
    provider.id_mr = 1
    provider.RE_HUNK_HEADER = RE_HUNK_HEADER
    provider.diff_files = [_patch_file()]
    provider.max_comment_chars = 65000
    provider.resolve_outdated_inline_threads = lambda: 0
    provider.reconcile_code_suggestion_threads = lambda: 0
    provider.get_relevant_diff = lambda *_args: SimpleNamespace(
        base_commit_sha="b", start_commit_sha="s", head_commit_sha="h"
    )
    provider.get_line_link = lambda f, s, e: f"https://gitlab.example/mr/1#{f}:{s}-{e}"
    provider.mr = MagicMock()
    provider.mr.discussions.create.side_effect = GitlabError(
        "line_code must be part of diff", response_code=400
    )
    command.git_provider = provider

    assert command.push_inline_docs({"Code Documentation": [{
        "relevant file": "app.py",
        "relevant line": 1,
        "doc placement": "after",
        "documentation": '"""Compute result."""',
    }]}) is True

    provider.mr.notes.create.assert_called_once()
    note_body = provider.mr.notes.create.call_args[0][0]["body"]
    assert "**Suggestion:** Proposed documentation" in note_body
    assert '```diff\n def compute(x):\n+    """Compute result."""\n```' in note_body


def test_push_inline_docs_renders_diff_block_for_bitbucket_before_placement():
    command = object.__new__(PRAddDocs)
    provider = object.__new__(BitbucketProvider)
    provider.diff_files = [_patch_file()]
    published = []
    provider.publish_inline_comments = lambda comments: (published.extend(comments), True)[1]
    command.git_provider = provider

    assert command.push_inline_docs({"Code Documentation": [{
        "relevant file": "app.py",
        "relevant line": 2,
        "doc placement": "before",
        "documentation": "# increment input",
    }]}) is True

    assert len(published) == 1
    assert "```suggestion" not in published[0]["body"]
    assert "```diff\n+    # increment input\n     y = x + 1\n```" in published[0]["body"]
