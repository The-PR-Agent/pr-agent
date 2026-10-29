import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from github import GithubException

from pr_agent.algo.run_details import command_failed, init_run_details, record_command_failure
from pr_agent.algo.types import FilePatchInfo
from pr_agent.algo.utils import convert_to_markdown_v2
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import IncrementalPR, PRCommit
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider
from pr_agent.tools.pr_reviewer import PRReviewer

COMMITS = [PRCommit("a" * 40, "first\n\nwhy"), PRCommit("b" * 40, "empty"), PRCommit("c" * 40, "third")]


def _reviewer(provider):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider
    reviewer.pr_url = "https://example/pr/1"
    reviewer.is_answer = False
    reviewer.is_auto = False
    reviewer.incremental = IncrementalPR(False)
    reviewer.vars = {"diff": "", "commit_under_review": ""}
    return reviewer


def _provider(changed_paths):
    provider = MagicMock()
    provider.get_pr_commits.return_value = list(COMMITS)
    provider.set_commit_review_scope.side_effect = lambda commit, incremental: changed_paths[commit.sha]
    return provider


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setattr(get_settings().pr_reviewer, "per_commit_review", True, raising=False)
    monkeypatch.setattr(get_settings().config, "publish_output", True, raising=False)
    monkeypatch.setattr(get_settings().config, "propagate_tool_errors", False, raising=False)
    monkeypatch.setattr(get_settings().config, "is_auto_command", False, raising=False)
    return get_settings()


@pytest.mark.asyncio
async def test_each_commit_with_changes_is_reviewed_with_its_own_message(settings):
    provider = _provider({"a" * 40: ["a.py"], "b" * 40: [], "c" * 40: ["c.py"]})
    reviewer = _reviewer(provider)
    seen = []

    async def review():
        seen.append((reviewer._review_commit.sha, reviewer.vars["commit_under_review"], reviewer.vars["diff"]))
        reviewer.vars["diff"] = "stale diff"

    with patch.object(reviewer, "_run_review", side_effect=review):
        await reviewer.run()

    assert seen == [("a" * 40, "first\n\nwhy", ""), ("c" * 40, "third", "")]
    assert reviewer._review_commit is None
    assert reviewer.vars == {"diff": "", "commit_under_review": ""}
    assert not reviewer.incremental.is_incremental
    provider.get_incremental_commits.assert_called_once_with(reviewer.incremental)


@pytest.mark.asyncio
async def test_a_failing_commit_does_not_stop_the_others_and_marks_the_run_failed(settings):
    provider = _provider({"a" * 40: ["a.py"], "b" * 40: ["b.py"], "c" * 40: ["c.py"]})
    provider.set_commit_review_scope.side_effect = [["a.py"], RuntimeError("api down"), ["c.py"]]
    reviewer = _reviewer(provider)
    reviewed = []

    async def review():
        init_run_details()
        reviewed.append(reviewer._review_commit.sha)
        if len(reviewed) == 1:
            record_command_failure()

    init_run_details()
    with patch.object(reviewer, "_run_review", side_effect=review):
        await reviewer.run()

    assert reviewed == ["a" * 40, "c" * 40]
    assert command_failed()


@pytest.mark.asyncio
async def test_failing_commit_error_propagates_after_the_rest_when_configured(settings, monkeypatch):
    monkeypatch.setattr(get_settings().config, "propagate_tool_errors", True, raising=False)
    provider = _provider({})
    provider.set_commit_review_scope.side_effect = [RuntimeError("boom"), ["b.py"], ["c.py"]]
    reviewer = _reviewer(provider)
    reviewed = []

    async def review():
        reviewed.append(reviewer._review_commit.sha)

    with patch.object(reviewer, "_run_review", side_effect=review), pytest.raises(RuntimeError, match="boom"):
        await reviewer.run()
    assert reviewed == ["b" * 40, "c" * 40]


@pytest.mark.asyncio
async def test_unsupported_provider_reviews_the_whole_pr_with_a_notice(settings):
    provider = MagicMock()
    provider.get_pr_commits.return_value = None
    reviewer = _reviewer(provider)

    with patch.object(reviewer, "_run_review") as run_review:
        await reviewer.run()

    run_review.assert_awaited_once()
    assert "not supported" in provider.publish_comment.call_args.args[0]
    provider.set_commit_review_scope.assert_not_called()


@pytest.mark.asyncio
async def test_more_commits_than_the_provider_lists_reviews_the_whole_pr_with_a_notice(settings):
    provider = _provider({})
    provider.pr.commits = 251
    reviewer = _reviewer(provider)

    with patch.object(reviewer, "_run_review") as run_review:
        await reviewer.run()

    run_review.assert_awaited_once()
    notice = provider.publish_comment.call_args.args[0]
    assert "251 commits" in notice and "3 the git provider API returns" in notice
    provider.set_commit_review_scope.assert_not_called()


@pytest.mark.asyncio
async def test_no_commit_with_changes_publishes_a_skip_notice(settings):
    provider = _provider({"a" * 40: [], "b" * 40: [], "c" * 40: []})
    reviewer = _reviewer(provider)

    with patch.object(reviewer, "_run_review") as run_review:
        await reviewer.run()

    run_review.assert_not_awaited()
    assert "skipped" in provider.publish_comment.call_args.args[0]


def _incremental(first_new_sha):
    incremental = IncrementalPR(True)
    incremental.commits_range = [] if first_new_sha is None else [SimpleNamespace(sha=first_new_sha)]
    incremental.first_new_commit = None if first_new_sha is None else SimpleNamespace(sha=first_new_sha)
    return incremental


@pytest.mark.parametrize(("first_new_sha", "expected"), [
    ("b" * 40, COMMITS[1:]),
    ("gone-after-force-push", COMMITS),
    (None, []),
])
def test_incremental_selects_commits_from_the_current_history(first_new_sha, expected):
    reviewer = _reviewer(MagicMock())
    reviewer.incremental = _incremental(first_new_sha)
    with patch.object(reviewer, "_can_run_incremental_review", return_value=True):
        assert reviewer._commits_to_review(list(COMMITS)) == expected


def test_incremental_threshold_skip_and_full_fallback():
    reviewer = _reviewer(MagicMock())
    reviewer.incremental = _incremental("b" * 40)
    with patch.object(reviewer, "_can_run_incremental_review", return_value=False):
        assert reviewer._commits_to_review(list(COMMITS)) is None

    def fall_back():
        reviewer.incremental.is_incremental = False
        return False

    with patch.object(reviewer, "_can_run_incremental_review", side_effect=fall_back):
        assert reviewer._commits_to_review(list(COMMITS)) == COMMITS


def test_commit_review_markdown_uses_incremental_header_and_names_the_commit():
    markdown = convert_to_markdown_v2({"review": {"security_concerns": "No"}}, commit_review="[`abc1234`](u) `fix`")
    assert markdown.startswith("## Incremental")
    assert "Review of commit [`abc1234`](u) `fix`." in markdown
    assert "since previous" not in markdown


def test_review_prompt_includes_the_commit_message_only_in_per_commit_mode(monkeypatch):
    from jinja2 import Environment, StrictUndefined, select_autoescape

    from tests.unittest.test_pr_reviewer_prompt_contract import _build_reviewer

    prompt_vars = _build_reviewer(monkeypatch).vars
    environment = Environment(autoescape=select_autoescape(default_for_string=False), undefined=StrictUndefined)
    template = environment.from_string(get_settings().pr_review_prompt.user)
    assert "COMMIT_MESSAGE_BEGIN" not in template.render(**prompt_vars)
    rendered = template.render(**dict(prompt_vars, commit_under_review="Fix the parser\n\nBecause X"))
    assert "<<<COMMIT_MESSAGE_BEGIN>>>\nFix the parser\n\nBecause X\n<<<COMMIT_MESSAGE_END>>>" in rendered


def _gitlab_provider():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.gl = MagicMock()
    provider.id_project = "group/repo"
    provider.diff_files = ["stale"]
    return provider


def _gitlab_commit(parent_ids, diffs):
    gl_commit = MagicMock(parent_ids=parent_ids)
    gl_commit.diff.return_value = diffs
    return gl_commit


def test_gitlab_scopes_the_diff_to_one_commit_against_its_parent():
    provider = _gitlab_provider()
    change = {"old_path": "a.py", "new_path": "a.py", "diff": "@@ -1 +1 @@\n-x\n+y\n",
              "new_file": False, "deleted_file": False, "renamed_file": False}
    project = provider.gl.projects.get.return_value
    project.commits.get.return_value = _gitlab_commit(["parent"], [change])
    incremental = IncrementalPR(True)

    assert provider.set_commit_review_scope(PRCommit("child", "msg"), incremental) == ["a.py"]
    assert provider.diff_files is None
    assert provider.get_files() == ["a.py"]
    assert (incremental.last_seen_commit_sha, incremental.first_new_commit_sha) == ("parent", "child")
    assert provider._incremental_head_sha == "child"
    project.commits.get.return_value.diff.assert_called_once_with(get_all=True)

    provider._expand_submodule_changes = lambda changes, refs: changes
    provider.get_pr_file_content = MagicMock(side_effect=lambda path, sha, **_: f"{sha}\n")
    provider.id_mr = 1
    [patch_info] = provider.get_diff_files()
    assert (patch_info.base_file, patch_info.head_file) == ("parent\n", "child\n")


@pytest.mark.parametrize("parent_ids", [[], ["p1", "p2"]])
def test_gitlab_merge_and_root_commits_have_no_own_diff(parent_ids):
    provider = _gitlab_provider()
    provider.gl.projects.get.return_value.commits.get.return_value = _gitlab_commit(parent_ids, [{"new_path": "a"}])

    assert provider.set_commit_review_scope(PRCommit("sha", "msg"), IncrementalPR(True)) == []
    assert provider.unreviewed_files_map == {}


def test_gitlab_lists_current_commits_oldest_first():
    provider = _gitlab_provider()
    provider.mr = MagicMock()
    provider.mr.commits.return_value = iter([SimpleNamespace(id="new", message="n", web_url="u2"),
                                             SimpleNamespace(id="old", message=None, web_url="u1")])
    assert provider.get_pr_commits() == [PRCommit("old", "", "u1"), PRCommit("new", "n", "u2")]


def _github_provider(parents, files):
    provider = GithubProvider.__new__(GithubProvider)
    provider.diff_files = ["stale"]
    gh_commit = MagicMock(parents=parents)
    gh_commit.get_files.return_value = files
    provider._get_repo = MagicMock(return_value=MagicMock(get_commit=MagicMock(return_value=gh_commit)))
    return provider


def test_github_scopes_content_to_the_commit_instead_of_the_pr_head():
    file = SimpleNamespace(filename="a.py", status="modified", patch="", previous_filename=None,
                           additions=1, deletions=1)
    provider = _github_provider([SimpleNamespace(sha="parent")], [file])
    incremental = IncrementalPR(True)

    assert provider.set_commit_review_scope(PRCommit("child", "msg"), incremental) == ["a.py"]
    assert (incremental.last_seen_commit_sha, incremental.head_sha) == ("parent", "child")

    provider.pr = SimpleNamespace(head=SimpleNamespace(sha="head"))
    provider.repo_obj = MagicMock()
    provider._get_pr_file_content = MagicMock(side_effect=lambda file, sha, path=None: f"{sha}\n")
    with patch("pr_agent.git_providers.github_provider.filter_ignored", side_effect=lambda files: files):
        [patch_info] = provider._get_diff_files()
    assert (patch_info.base_file, patch_info.head_file) == ("parent\n", "child\n")


def test_github_merge_commit_has_no_own_diff():
    provider = _github_provider([SimpleNamespace(sha="p1"), SimpleNamespace(sha="p2")], [SimpleNamespace()])
    assert provider.set_commit_review_scope(PRCommit("sha", "msg"), IncrementalPR(True)) == []
    assert provider.unreviewed_files_map == {}


def _commit_reviewer(monkeypatch, prediction):
    from pr_agent.tools import pr_reviewer as pr_reviewer_module
    from tests.unittest.test_pr_reviewer_prompt_contract import _build_reviewer

    reviewer = _build_reviewer(monkeypatch)
    provider = reviewer.git_provider
    provider.get_pr_commits.return_value = [PRCommit("a" * 40, "Add parser\n\nbody", "https://host/c/a")]
    provider.set_commit_review_scope.return_value = ["a.py"]
    provider.get_files.return_value = ["a.py"]
    provider.supports_review_comment_identity.return_value = True
    provider.should_publish_review_as_thread.return_value = False
    provider.get_pr_url.return_value = "https://host/pr/1"
    provider.get_pr_labels.return_value = []
    provider.get_diff_files.return_value = [FilePatchInfo(base_file="", head_file="x\n", patch="", filename="a.py")]
    provider._inline_comment_store = None
    provider.get_persistent_comment_bodies.return_value = []
    provider.get_recent_inline_comment_bodies.return_value = []

    async def predict(prepare, **_):
        reviewer.prediction = prediction

    monkeypatch.setattr(pr_reviewer_module, "retry_with_fallback_models", predict)
    monkeypatch.setattr(pr_reviewer_module, "extract_and_cache_pr_tickets", MagicMock(side_effect=lambda *_: _noop()))
    monkeypatch.setattr(pr_reviewer_module, "add_pr_review_identity", lambda body, marker, _: f"{body}\n{marker}")
    return reviewer


@pytest.mark.asyncio
async def test_per_commit_reviews_publish_labelled_incremental_comments_not_the_persistent_one(settings, monkeypatch):
    from pr_agent.algo.comment_identity import PRReviewIdentity

    monkeypatch.setattr(get_settings().pr_reviewer, "inline_key_issues", True, raising=False)
    reviewer = _commit_reviewer(monkeypatch, "review:\n  security_concerns: 'SQL injection: raw query'\n"
                                             "  key_issues_to_review:\n  - relevant_file: a.py\n    issue_header: Bug\n"
                                             "    issue_content: Broken\n    start_line: 1\n    end_line: 1\n")
    provider = reviewer.git_provider
    await reviewer.run()

    bodies = [call.args[0] for call in provider.publish_comment.call_args_list]
    review = next(body for body in bodies if "Review of commit" in body)
    assert "[`aaaaaaa`](https://host/c/a) `Add parser`" in review
    [suggestions] = provider.publish_code_suggestions.call_args.args
    assert [suggestion["relevant_file"] for suggestion in suggestions] == ["a.py"]
    assert PRReviewIdentity.INCREMENTAL.value in review
    provider.publish_persistent_comment.assert_not_called()
    provider.publish_labels.assert_not_called()
    assert reviewer._review_commit is None


@pytest.mark.asyncio
@pytest.mark.parametrize("key_issues", ["  key_issues_to_review: []\n", "  key_issues_to_review: No\n", "",
                                        "  key_issues_to_review: No major issues detected in this commit\n",
                                        "  key_issues_to_review:\n  - {}\n"])
@pytest.mark.parametrize("publish_no_suggestions", [False, True])
async def test_commit_without_findings_publishes_only_when_configured(settings, monkeypatch, key_issues,
                                                                      publish_no_suggestions):
    monkeypatch.setattr(get_settings().pr_reviewer, "publish_output_no_suggestions", publish_no_suggestions)
    monkeypatch.setattr(get_settings().pr_reviewer, "inline_key_issues", False, raising=False)
    reviewer = _commit_reviewer(monkeypatch, "review:\n  estimated_effort_to_review_[1-5]: 1\n"
                                             f"  security_concerns: No\n{key_issues}")
    await reviewer.run()

    bodies = [call.args[0] for call in reviewer.git_provider.publish_comment.call_args_list]
    assert any("Review of commit" in body for body in bodies) is publish_no_suggestions
    reviewer.git_provider.publish_persistent_comment.assert_not_called()


async def _noop():
    return None


def _key_issue(start_line, content):
    return {"relevant_file": "a.py", "issue_header": "Possible Issue", "issue_content": content,
            "start_line": start_line, "end_line": start_line}


@pytest.fixture
def inline_settings(monkeypatch):
    monkeypatch.setattr(get_settings().config, "persistent_inline_comments", False, raising=False)
    monkeypatch.setattr(get_settings().github, "try_fix_invalid_inline_comments", False, raising=False)
    monkeypatch.setattr(get_settings().gitlab, "resolve_outdated_inline_threads", True, raising=False)
    monkeypatch.setattr(get_settings().gitlab, "publish_code_suggestions_as_review", False, raising=False)


def test_github_inline_findings_anchor_on_the_commit_and_rejected_ones_stay_in_the_summary(inline_settings):
    file = SimpleNamespace(filename="a.py", status="modified", patch="", previous_filename=None,
                           additions=1, deletions=1)
    provider = _github_provider([SimpleNamespace(sha="parent")], [file])
    head = SimpleNamespace(sha="head")
    provider.last_commit_id = head
    provider.set_commit_review_scope(PRCommit("child", "msg"), IncrementalPR(True))
    gh_commit = provider._get_repo().get_commit.return_value
    gh_commit.sha = "child"
    provider.pr = MagicMock(url="https://api/pulls/1")
    provider.max_comment_chars = 65000
    provider.get_diff_files = MagicMock(return_value=[
        FilePatchInfo(base_file="1\n", head_file="1\n2\n3\n4\n5\n", patch="@@ -1 +1,2 @@\n 1\n+2\n",
                      filename="a.py")])
    provider.pr.create_review.side_effect = [GithubException(422, {}, None), None]

    def verify(method, url, input=None):
        if method == "POST" and input["comments"][0]["line"] == 5:
            raise GithubException(422, {}, None)
        return {}, {"id": 7}

    provider.pr._requester.requestJsonAndCheck.side_effect = verify
    reviewer = _reviewer(provider)
    anchored, unanchored = _key_issue(2, "Lock is never released."), _key_issue(5, "Line outside the commit.")

    with patch("pr_agent.git_providers.github_provider.time.sleep"):
        result = reviewer._publish_key_issues_as_inline_comments({"review": {"key_issues_to_review": [anchored,
                                                                                                    unanchored]}})

    assert [call.kwargs["commit"] for call in provider.pr.create_review.call_args_list] == [gh_commit, gh_commit]
    [published] = provider.pr.create_review.call_args.kwargs["comments"]
    assert published["line"] == 2 and "Lock is never released." in published["body"]
    posts = [call for call in provider.pr._requester.requestJsonAndCheck.call_args_list if call.args[0] == "POST"]
    assert {call.kwargs["input"]["commit_id"] for call in posts} == {"child"}
    assert result["review"]["key_issues_to_review"] == [unanchored]

    provider.get_incremental_commits(IncrementalPR(False))
    assert provider.last_commit_id is head


def test_gitlab_inline_findings_anchor_on_the_commit_diff_refs(inline_settings):
    provider = _gitlab_provider()
    change = {"old_path": "a.py", "new_path": "a.py", "diff": "@@ -1 +1 @@\n-x\n+y\n",
              "new_file": False, "deleted_file": False, "renamed_file": False}
    provider.gl.projects.get.return_value.commits.get.return_value = _gitlab_commit(["parent"], [change])
    provider.set_commit_review_scope(PRCommit("child", "msg"), IncrementalPR(True))
    provider.mr = MagicMock()
    provider.id_mr = 1
    provider.RE_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[ ]?(.*)")
    provider.get_diff_files = MagicMock(return_value=[
        FilePatchInfo(base_file="x\n", head_file="y\n", patch=change["diff"], filename="a.py")])

    assert provider.publish_code_suggestions([{"body": "finding", "relevant_file": "a.py",
                                               "relevant_lines_start": 1, "relevant_lines_end": 1}])

    position = provider.mr.discussions.create.call_args.args[0]["position"]
    assert (position["base_sha"], position["start_sha"], position["head_sha"]) == ("parent", "parent", "child")
    assert position["new_line"] == 1
    provider.mr.discussions.list.assert_not_called()

    provider.get_incremental_commits(IncrementalPR(False))
    assert provider._commit_diff_refs is None
