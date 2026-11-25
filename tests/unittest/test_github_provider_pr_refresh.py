"""A provider shared by the commands of one run re-reads the pull request before each of them."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from github import GithubException
from starlette_context import context, request_cycle_context

from pr_agent.git_providers import github_provider as gp


def _pr(head="h1", *, changed_files=1, labels=(), commits=None, body=""):
    pr = SimpleNamespace(
        head=SimpleNamespace(sha=head, ref="feature"),
        base=SimpleNamespace(sha="b1", ref="main"),
        changed_files=changed_files,
        labels=[SimpleNamespace(name=name) for name in labels],
        title="Add feature",
        body=body,
        user=SimpleNamespace(login="alice"),
    )
    commit_shas = (head,) if commits is None else commits
    pr.get_commits = Mock(return_value=[SimpleNamespace(sha=sha) for sha in commit_shas])
    return pr


def _provider(loaded):
    provider = gp.GithubProvider.__new__(gp.GithubProvider)
    provider.repo = "org/repo"
    provider.pr = loaded
    provider.pr_commits, provider.last_commit_id = provider._read_pr_commits(loaded)
    provider._pr_revision_loaded = provider._pr_revision(loaded)
    provider.git_files = ["loaded-file"]
    provider.comments = ["loaded-comment"]
    return provider


def test_refresh_reads_labels_and_comments_again_and_keeps_the_lists_of_an_unmoved_head():
    provider = _provider(_pr())
    fresh = _pr(labels=["Possible security concern"])
    provider._get_pr = Mock(return_value=fresh)
    files, commits = provider.git_files, provider.pr_commits

    provider.refresh_pr_for_command()

    assert provider.pr is fresh
    assert provider.get_request_policy_metadata({"labels"})["labels"] == ["Possible security concern"]
    assert provider.comments is None
    assert provider.git_files is files and provider.pr_commits is commits
    fresh.get_commits.assert_not_called()


def test_refresh_reads_the_description_again():
    provider = _provider(_pr(body="Add feature"))
    assert provider.get_user_description() == "Add feature"
    provider._get_pr = Mock(return_value=_pr(body="Add feature\n\nFixes #12"))

    provider.refresh_pr_for_command()

    assert provider.get_user_description() == "Add feature\n\nFixes #12"


@pytest.mark.parametrize("moved_on_loaded_object", [False, True])
def test_refresh_reads_the_lists_again_when_the_head_moved(moved_on_loaded_object):
    loaded = _pr("h1")
    provider = _provider(loaded)
    if moved_on_loaded_object:
        # PullRequest.edit() copies the head GitHub reports onto the object the provider already holds.
        loaded.head = SimpleNamespace(sha="h2", ref="feature")
        loaded.changed_files = 2
        loaded.get_commits = Mock(return_value=[SimpleNamespace(sha="h1"), SimpleNamespace(sha="h2")])
        fresh = loaded
    else:
        fresh = _pr("h2", changed_files=2, commits=["h1", "h2"])
    provider._get_pr = Mock(return_value=fresh)

    with request_cycle_context({"git_files": ["loaded-file"]}):
        provider.refresh_pr_for_command()
        cached_for_the_request = context.get("git_files")

    assert provider.git_files is None and cached_for_the_request is None
    assert [commit.sha for commit in provider.pr_commits] == ["h1", "h2"]
    assert provider.last_commit_id is provider.pr_commits[-1]
    assert provider._pr_revision_loaded == ("h2", "b1", 2)


@pytest.mark.parametrize(
    ("head", "changed_files", "requests"),
    [
        ("h1", 1, [("PATCH", "check-runs/111")]),
        ("h2", 2, [("GET", "commits/h2/check-runs"), ("POST", "check-runs")]),
    ],
)
def test_refresh_reuses_a_check_run_only_while_the_head_is_the_one_it_was_opened_on(head, changed_files, requests):
    provider = _provider(_pr("h1"))
    provider.base_url = "https://api.github.com"
    provider._check_run_ids = {"review": 111}
    provider._check_runs_in_progress = set()
    provider._check_run_base_summaries = {}
    provider._check_runs_progress_blocked = set()
    fresh = _pr(head, changed_files=changed_files)
    fresh._requester = Mock()
    fresh._requester.requestJsonAndCheck.side_effect = lambda method, url, **kwargs: (
        ({}, {"check_runs": []}) if method == "GET" else ({}, {"id": 222})
    )
    provider._get_pr = Mock(return_value=fresh)

    provider.refresh_pr_for_command()
    assert provider._publish_check_run("review output", "review") is True

    calls = fresh._requester.requestJsonAndCheck.call_args_list
    assert [(call.args[0], call.args[1].rsplit("/repos/org/repo/", 1)[-1]) for call in calls] == requests


@pytest.mark.parametrize("head_moved_by_edit", [False, True])
@pytest.mark.parametrize("failing_read", ["pull request", "commits", "head commit"])
def test_refresh_raises_and_assigns_nothing_when_github_cannot_be_reached(failing_read, head_moved_by_edit):
    loaded = _pr("h1")
    provider = _provider(loaded)
    if head_moved_by_edit:
        # PullRequest.edit() copied the new head onto the loaded object; the lists are still those of "h1".
        loaded.head = SimpleNamespace(sha="h2", ref="feature")
    fresh = _pr("h2", changed_files=2, commits=["h1", "h2"])
    error = GithubException(502, {"message": "Bad Gateway"}, {})
    if failing_read == "pull request":
        provider._get_pr = Mock(side_effect=error)
    else:
        provider._get_pr = Mock(return_value=fresh)
        if failing_read == "commits":
            fresh.get_commits.side_effect = error
        else:
            fresh.get_commits.return_value = [SimpleNamespace(sha="h1")]
            provider._get_repo = Mock()
            provider._get_repo.return_value.get_commit.side_effect = error
    files, commits, comments = provider.git_files, provider.pr_commits, provider.comments
    last_commit = provider.last_commit_id

    with pytest.raises(GithubException):
        provider.refresh_pr_for_command()

    assert provider.pr is loaded
    assert provider.git_files is files and provider.pr_commits is commits and provider.comments is comments
    assert provider.last_commit_id is last_commit
    assert provider._pr_revision_loaded == ("h1", "b1", 1)



def _commit(sha, message):
    return SimpleNamespace(sha=sha, commit=SimpleNamespace(message=message))


def _with_commits(pr, commits):
    pr.commits = len(commits)
    pr.get_commits.return_value = commits
    return pr


def test_commit_messages_reuse_the_commits_read_for_the_current_head():
    loaded = _with_commits(_pr("h1"), [_commit("h1", "Add feature")])
    provider = _provider(loaded)
    moved = _with_commits(_pr("h2", changed_files=2), [_commit("h1", "Add feature"), _commit("h2", "Fix typo")])
    provider._get_pr = Mock(return_value=moved)

    assert provider.get_commit_messages() == "1. Add feature"
    provider.refresh_pr_for_command()
    assert provider.get_commit_messages() == "1. Add feature\n2. Fix typo"

    loaded.get_commits.assert_called_once()
    moved.get_commits.assert_called_once()


@pytest.mark.parametrize(
    ("loaded_shas", "reported", "base_moved", "reads_again"),
    [
        (["h1"], 2, False, True),  # the list was read before it showed the head
        (["h0", "h2"], 3, False, True),  # a commit is missing although the head is listed
        (["h1"], 1, False, True),  # the count matches but the list ends before the head
        (["h2"], 1, True, True),  # PullRequest.edit() reported a new base for the loaded PR
        ([f"c{i}" for i in range(250)], 300, False, True),  # past GitHub's cap the list cannot be checked
        (["h1", "h2"], 2, False, False),  # complete for the current revision
    ],
)
def test_commit_messages_read_the_commits_again_unless_the_loaded_list_matches_the_pr(
    loaded_shas, reported, base_moved, reads_again,
):
    loaded = _with_commits(_pr("h2"), [_commit("h2", "Fresh read")])
    loaded.commits = reported
    provider = _provider(loaded)
    provider.pr_commits = [_commit(sha, "Loaded") for sha in loaded_shas]
    if base_moved:
        loaded.base = SimpleNamespace(sha="b2", ref="release")

    messages = provider.get_commit_messages()

    loaded_messages = "\n".join(f"{i + 1}. Loaded" for i in range(len(loaded_shas)))
    assert messages == ("1. Fresh read" if reads_again else loaded_messages)
    assert loaded.get_commits.call_count == 1 + reads_again


def test_commit_messages_are_read_again_after_switching_pull_requests():
    # Same head and count as the loaded PR, so only dropping its commits makes the new PR's messages appear.
    provider = _provider(_with_commits(_pr("h1"), [_commit("h1", "Add feature")]))
    provider.pr_num = 1
    other = _with_commits(_pr("h1"), [_commit("h1", "Other change")])
    provider._get_pr = Mock(return_value=other)

    provider.set_pr("https://github.com/org/repo/pull/2")

    assert provider.get_commit_messages() == "1. Other change"
