"""
Tests for GitHub provider PR description publishing.

GitHub refuses a pull request body over 65,536 characters with a 422 ("body is too long"), and the
file walkthrough /describe renders for a large PR can pass that. ``publish_description`` fits the
body into the limit instead of losing the whole description.
"""

import pytest
from github import GithubException

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.commands.pr_description import PRDescription
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.github_provider import GithubProvider
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

GITHUB_BODY_LIMIT = 65536
USAGE_GUIDE = (
    "<hr>\n\n<details> <summary><strong>✨ Describe command usage guide:</strong></summary><hr> \n\n"
    "Usage guide text.\n</details>\n"
)


class _FakePullRequest:
    """Records edits, and refuses a body over the limit as GitHub does."""

    def __init__(self):
        self.edits = []

    def edit(self, **fields):
        body = fields.get("body")
        if body is not None and len(body) > GITHUB_BODY_LIMIT:
            raise GithubException(422, {"message": "Validation Failed", "errors": [{
                "resource": "PullRequest", "code": "custom", "field": "body",
                "message": f"body is too long (maximum is {GITHUB_BODY_LIMIT} characters)"}]}, {})
        self.edits.append(fields)


def _make_provider(paths=()):
    provider = GithubProvider.__new__(GithubProvider)
    provider.pr = _FakePullRequest()
    provider.repo = "owner/repo"
    provider.pr_num = 1
    provider.base_url_html = "https://github.com"
    provider.max_comment_chars = 65000
    provider.diff_files = [
        FilePatchInfo("", "", "@@ -1 +1 @@\n-old\n+new", path, edit_type=EDIT_TYPE.MODIFIED,
                      num_plus_lines=12, num_minus_lines=9)
        for path in paths
    ]
    return provider


def _describe_body(provider, num_described):
    """Render the body /describe publishes for the provider's diff files, as PRDescription.run() builds it."""
    paths = [file.filename for file in provider.diff_files]
    description = PRDescription.__new__(PRDescription)
    description.git_provider = provider
    description.pr_id = "owner/repo/1"
    description.COLLAPSIBLE_FILE_LIST_THRESHOLD = 6
    # Over collapsible_file_list_threshold files, /describe asks for no per-file summaries.
    description.vars = {"title": "Rename the billing modules", "include_file_summary_changes": False}
    pr_files = [
        {"filename": path, "changes_title": "Rename the module and update its imports", "label": "enhancement"}
        for path in paths[:num_described]
    ]
    # extend_uncovered_files lists up to 100 of the files the model did not describe.
    pr_files += [
        {"filename": path, "changes_title": "...", "label": "additional files"}
        for path in paths[num_described:num_described + 100]
    ]
    description.data = {
        "type": ["Enhancement"],
        "description": "- Rename the billing modules\n- Update their imports",
        "pr_files": pr_files,
    }
    description.file_label_dict = description._prepare_file_labels()
    _, body, walkthrough = description._prepare_pr_answer()
    return "<!-- pr-agent-generated -->\n" + body + "\n\n" + walkthrough + "___\n\n"


def _large_pr_provider():
    paths = [f"services/billing/src/main/java/com/example/billing/rules/InvoiceRule{i:03}.java" for i in range(230)]
    return _make_provider(paths)


def test_publish_description_keeps_a_body_within_the_limit():
    provider = _make_provider(["src/app.py"])
    body = _describe_body(provider, num_described=1)

    provider.publish_description("AI title", body)

    assert provider.pr.edits == [{"title": "AI title", "body": body}]


@pytest.mark.parametrize("open_by_default", [False, True])
@pytest.mark.parametrize("title", ["AI title", None])
def test_publish_description_drops_the_file_walkthrough_of_a_large_pr(title, open_by_default):
    """230 changed files, 120 of them described across the large-PR chunks: a body of about 71,600 characters."""
    settings_snapshot = snapshot_settings(["pr_description.file_table_collapsible_open_by_default"])
    try:
        get_settings().set("pr_description.file_table_collapsible_open_by_default", open_by_default)
        provider = _large_pr_provider()
        body = _describe_body(provider, num_described=120)
    finally:
        restore_settings(settings_snapshot)
    assert len(body) > GITHUB_BODY_LIMIT
    opener = "<details open>" if open_by_default else "<details>"
    summary = body[:body.index(f"{opener} <summary><h3> File Walkthrough</h3></summary>")]

    provider.publish_description(title, body)

    expected = {"body": summary} if title is None else {"title": title, "body": summary}
    assert provider.pr.edits == [expected]
    assert "### **PR Type**\nEnhancement" in summary
    assert "- Rename the billing modules" in summary


def test_publish_description_drops_the_usage_guide_before_the_file_walkthrough():
    provider = _make_provider(["src/app.py", "src/billing.py"])
    body = _describe_body(provider, num_described=2)
    provider.max_comment_chars = len(body + USAGE_GUIDE) - 1

    provider.publish_description("AI title", body + USAGE_GUIDE)

    assert provider.pr.edits == [{"title": "AI title", "body": body + "<hr>\n\n"}]


def test_publish_description_cuts_a_long_body_without_a_file_walkthrough():
    """A body with no walkthrough to drop, such as a long template filled through description markers."""
    provider = _make_provider()
    body = "## Summary\n" + "Billing rules moved to the shared rounding helper.\n" * 2000

    provider.publish_description(None, body)

    published = provider.pr.edits[0]["body"]
    suffix = " ... (description truncated due to length limit)"
    assert published == body[:provider.max_comment_chars - len(suffix)] + suffix
    assert len(published) == provider.max_comment_chars
