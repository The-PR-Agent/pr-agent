from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from github import GithubException

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

# ---------------------------------------------------------------------------
# 1. Base GitProvider defaults
# ---------------------------------------------------------------------------

class _DummyGitProvider(GitProvider):
    pass

_DummyGitProvider.__abstractmethods__ = frozenset()


def test_base_git_provider_defaults():
    provider = _DummyGitProvider()
    assert provider.get_head_ci_status() is None
    assert provider.get_head_ci_status("abc1234") is None
    assert provider.is_head_stale_against_base() is None
    assert provider.are_all_authors_human() is None


# ---------------------------------------------------------------------------
# 2. Capability checks across providers
# ---------------------------------------------------------------------------

def test_capabilities_supported_on_github_and_gitlab():
    gh = GithubProvider.__new__(GithubProvider)
    gl = GitLabProvider.__new__(GitLabProvider)

    for cap in ("head_ci_status", "head_staleness", "author_human_check"):
        assert gh.is_supported(cap) is True
        assert gl.is_supported(cap) is True


def test_capabilities_declined_on_other_providers():
    other_classes = [
        AzureDevopsProvider,
        BitbucketProvider,
        BitbucketServerProvider,
        CodeCommitProvider,
        GerritProvider,
        GiteaProvider,
        LocalGitProvider,
        PlainDiffGitProvider,
    ]
    for cls in other_classes:
        provider = cls.__new__(cls)
        for cap in ("head_ci_status", "head_staleness", "author_human_check"):
            assert provider.is_supported(cap) is False, f"{cls.__name__} should decline {cap}"


# ---------------------------------------------------------------------------
# 3. GithubProvider.get_head_ci_status
# ---------------------------------------------------------------------------

def _make_github_provider(
    head_sha="head123",
    check_runs=None,
    combined_status=None,
    pr=None,
    repo_obj=None,
):
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "owner/repo"
    provider.base_url = "https://api.github.com"
    if pr is None:
        pr = SimpleNamespace(head=SimpleNamespace(sha=head_sha))
    provider.pr = pr

    if repo_obj is None:
        repo_obj = MagicMock()
        commit_mock = MagicMock()
        commit_mock.get_check_runs.return_value = check_runs if check_runs is not None else []
        if combined_status is not None:
            commit_mock.get_combined_status.return_value = combined_status
        else:
            commit_mock.get_combined_status.return_value = SimpleNamespace(total_count=0, state="success")
        repo_obj.get_commit.return_value = commit_mock

    provider.repo_obj = repo_obj
    return provider


def test_github_ci_status_none_when_no_checks_or_statuses():
    provider = _make_github_provider(check_runs=[], combined_status=SimpleNamespace(total_count=0, state="pending"))
    assert provider.get_head_ci_status() == "none"


def test_github_ci_status_passed_when_all_check_runs_succeed():
    runs = [
        SimpleNamespace(status="completed", conclusion="success", app=None, name="test"),
        SimpleNamespace(status="completed", conclusion="neutral", app=None, name="lint"),
        SimpleNamespace(status="completed", conclusion="skipped", app=None, name="deploy"),
    ]
    provider = _make_github_provider(check_runs=runs)
    assert provider.get_head_ci_status() == "passed"


def test_github_ci_status_failed_on_check_run_failure_conclusions():
    for conclusion in ("failure", "timed_out", "cancelled", "action_required", "stale"):
        runs = [
            SimpleNamespace(status="completed", conclusion="success", app=None, name="test"),
            SimpleNamespace(status="completed", conclusion=conclusion, app=None, name="build"),
        ]
        provider = _make_github_provider(check_runs=runs)
        assert provider.get_head_ci_status() == "failed", f"Expected failed for {conclusion}"


def test_github_ci_status_pending_when_check_run_in_progress():
    runs = [
        SimpleNamespace(status="completed", conclusion="success", app=None, name="test"),
        SimpleNamespace(status="in_progress", conclusion=None, app=None, name="build"),
    ]
    provider = _make_github_provider(check_runs=runs)
    assert provider.get_head_ci_status() == "pending"

    runs_queued = [
        SimpleNamespace(status="queued", conclusion=None, app=None, name="e2e"),
    ]
    provider_queued = _make_github_provider(check_runs=runs_queued)
    assert provider_queued.get_head_ci_status() == "pending"


def test_github_ci_status_ignores_pr_agent_own_check_runs():
    pr_agent_app = SimpleNamespace(slug="pr-agent", name="PR Agent")
    runs = [
        # PR-Agent review run in progress should not mark CI pending
        SimpleNamespace(status="in_progress", conclusion=None, app=pr_agent_app, name="PR Agent - Review"),
        SimpleNamespace(status="completed", conclusion="success", app=None, name="ci/build"),
    ]
    provider = _make_github_provider(check_runs=runs)
    assert provider.get_head_ci_status() == "passed"


def test_github_ci_status_returns_none_if_only_pr_agent_runs_exist():
    pr_agent_app = SimpleNamespace(slug="pr-agent", name="PR Agent")
    runs = [
        SimpleNamespace(status="in_progress", conclusion=None, app=pr_agent_app, name="PR Agent - Review"),
    ]
    provider = _make_github_provider(check_runs=runs)
    assert provider.get_head_ci_status() == "none"


def test_github_ci_status_from_combined_status():
    # Only combined status present
    provider_pass = _make_github_provider(
        check_runs=[],
        combined_status=SimpleNamespace(total_count=1, state="success"),
    )
    assert provider_pass.get_head_ci_status() == "passed"

    provider_fail = _make_github_provider(
        check_runs=[],
        combined_status=SimpleNamespace(total_count=1, state="failure"),
    )
    assert provider_fail.get_head_ci_status() == "failed"

    provider_err = _make_github_provider(
        check_runs=[],
        combined_status=SimpleNamespace(total_count=1, state="error"),
    )
    assert provider_err.get_head_ci_status() == "failed"

    provider_pending = _make_github_provider(
        check_runs=[],
        combined_status=SimpleNamespace(total_count=1, state="pending"),
    )
    assert provider_pending.get_head_ci_status() == "pending"


def test_github_ci_status_failure_takes_precedence_over_pending():
    runs = [
        SimpleNamespace(status="in_progress", conclusion=None, app=None, name="test"),
        SimpleNamespace(status="completed", conclusion="failure", app=None, name="lint"),
    ]
    provider = _make_github_provider(check_runs=runs)
    assert provider.get_head_ci_status() == "failed"


def test_github_ci_status_returns_none_on_missing_sha_or_api_error():
    provider_no_sha = GithubProvider.__new__(GithubProvider)
    provider_no_sha.pr = SimpleNamespace(head=None)
    assert provider_no_sha.get_head_ci_status() is None

    repo_error = MagicMock()
    repo_error.get_commit.side_effect = GithubException(404, "Not Found", {})
    provider_err = _make_github_provider(repo_obj=repo_error)
    assert provider_err.get_head_ci_status() is None


# ---------------------------------------------------------------------------
# 4. GithubProvider.is_head_stale_against_base
# ---------------------------------------------------------------------------

def test_github_staleness_returns_false_when_no_diff_files():
    provider = _make_github_provider()
    provider.get_diff_files = lambda: []
    assert provider.is_head_stale_against_base() is False


def test_github_staleness_returns_false_when_no_base_commits_since_ci():
    now = datetime.now(UTC)
    runs = [SimpleNamespace(status="completed", conclusion="success", app=None, completed_at=now, name="test")]
    provider = _make_github_provider(check_runs=runs)
    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]
    provider.pr.base = SimpleNamespace(ref="main")

    provider.repo_obj.get_commits.return_value = []
    assert provider.is_head_stale_against_base() is False


def test_github_staleness_returns_false_when_base_commits_touch_unrelated_files():
    now = datetime.now(UTC)
    runs = [SimpleNamespace(status="completed", conclusion="success", app=None, completed_at=now, name="test")]
    provider = _make_github_provider(check_runs=runs)
    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]
    provider.pr.base = SimpleNamespace(ref="main")

    base_commit = SimpleNamespace(
        commit=SimpleNamespace(committer=SimpleNamespace(date=now + timedelta(minutes=5))),
        files=[SimpleNamespace(filename="docs/readme.md")],
    )
    provider.repo_obj.get_commits.return_value = [base_commit]
    assert provider.is_head_stale_against_base() is False


def test_github_staleness_returns_true_when_base_commit_touches_pr_file():
    now = datetime.now(UTC)
    runs = [SimpleNamespace(status="completed", conclusion="success", app=None, completed_at=now, name="test")]
    provider = _make_github_provider(check_runs=runs)
    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]
    provider.pr.base = SimpleNamespace(ref="main")

    base_commit = SimpleNamespace(
        commit=SimpleNamespace(committer=SimpleNamespace(date=now + timedelta(minutes=5))),
        files=[SimpleNamespace(filename="app/main.py")],
    )
    provider.repo_obj.get_commits.return_value = [base_commit]
    assert provider.is_head_stale_against_base() is True


def test_github_staleness_handles_naive_and_aware_datetimes():
    # Naive datetime in run completed_at vs aware datetime in commit committer
    naive_dt = datetime(2026, 1, 1, 12, 0, 0)
    aware_dt = datetime(2026, 1, 1, 12, 10, 0, tzinfo=UTC)

    runs = [SimpleNamespace(status="completed", conclusion="success", app=None, completed_at=naive_dt, name="test")]
    provider = _make_github_provider(check_runs=runs)
    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]
    provider.pr.base = SimpleNamespace(ref="main")

    base_commit = SimpleNamespace(
        commit=SimpleNamespace(committer=SimpleNamespace(date=aware_dt)),
        files=[SimpleNamespace(filename="app/main.py")],
    )
    provider.repo_obj.get_commits.return_value = [base_commit]
    assert provider.is_head_stale_against_base() is True


def test_github_staleness_falls_back_to_head_commit_date_when_no_ci_runs():
    now = datetime.now(UTC)
    provider = _make_github_provider(check_runs=[])
    # Configure commit timestamp on head
    head_commit = provider.repo_obj.get_commit.return_value
    head_commit.commit = SimpleNamespace(committer=SimpleNamespace(date=now))

    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]
    provider.pr.base = SimpleNamespace(ref="main")

    base_commit = SimpleNamespace(
        commit=SimpleNamespace(committer=SimpleNamespace(date=now + timedelta(minutes=5))),
        files=[SimpleNamespace(filename="app/main.py")],
    )
    provider.repo_obj.get_commits.return_value = [base_commit]
    assert provider.is_head_stale_against_base() is True


# ---------------------------------------------------------------------------
# 5. GithubProvider.are_all_authors_human
# ---------------------------------------------------------------------------

def test_github_authors_human_when_all_users():
    pr = SimpleNamespace(
        head=SimpleNamespace(sha="head123"),
        user=SimpleNamespace(type="User", login="octocat"),
    )
    commits = [
        SimpleNamespace(
            author=SimpleNamespace(type="User", login="octocat"),
            commit=SimpleNamespace(author=SimpleNamespace(name="Octo Cat", email="octo@example.com")),
        ),
        SimpleNamespace(
            author=SimpleNamespace(type="User", login="collaborator"),
            commit=SimpleNamespace(author=SimpleNamespace(name="Collab", email="collab@example.com")),
        ),
    ]
    pr.get_commits = lambda: commits
    provider = _make_github_provider(pr=pr)
    assert provider.are_all_authors_human() is True


def test_github_authors_false_when_pr_author_is_bot():
    pr = SimpleNamespace(
        head=SimpleNamespace(sha="head123"),
        user=SimpleNamespace(type="Bot", login="dependabot[bot]"),
    )
    pr.get_commits = lambda: []
    provider = _make_github_provider(pr=pr)
    assert provider.are_all_authors_human() is False


def test_github_authors_false_when_commit_author_is_bot():
    pr = SimpleNamespace(
        head=SimpleNamespace(sha="head123"),
        user=SimpleNamespace(type="User", login="octocat"),
    )
    commits = [
        SimpleNamespace(
            author=SimpleNamespace(type="User", login="octocat"),
            commit=SimpleNamespace(author=SimpleNamespace(name="Octo", email="octo@example.com")),
        ),
        SimpleNamespace(
            author=SimpleNamespace(type="Bot", login="github-actions[bot]"),
            commit=SimpleNamespace(author=SimpleNamespace(name="GH Actions", email="actions@github.com")),
        ),
    ]
    pr.get_commits = lambda: commits
    provider = _make_github_provider(pr=pr)
    assert provider.are_all_authors_human() is False


def test_github_authors_false_when_commit_author_is_none():
    pr = SimpleNamespace(
        head=SimpleNamespace(sha="head123"),
        user=SimpleNamespace(type="User", login="octocat"),
    )
    commits = [
        SimpleNamespace(
            author=None,
            commit=SimpleNamespace(author=SimpleNamespace(name="Unknown", email="unknown@nowhere.local")),
        ),
    ]
    pr.get_commits = lambda: commits
    provider = _make_github_provider(pr=pr)
    assert provider.are_all_authors_human() is False


def test_github_authors_false_when_git_commit_email_is_bot_noreply():
    pr = SimpleNamespace(
        head=SimpleNamespace(sha="head123"),
        user=SimpleNamespace(type="User", login="octocat"),
    )
    commits = [
        SimpleNamespace(
            author=SimpleNamespace(type="User", login="octocat"),
            commit=SimpleNamespace(
                author=SimpleNamespace(name="Renovate", email="29139614+renovate[bot]@users.noreply.github.com")
            ),
        ),
    ]
    pr.get_commits = lambda: commits
    provider = _make_github_provider(pr=pr)
    assert provider.are_all_authors_human() is False


# ---------------------------------------------------------------------------
# 6. GitLabProvider.get_head_ci_status
# ---------------------------------------------------------------------------

def _make_gitlab_provider(head_pipeline=None, pipelines=None, head_sha="glhead123"):
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "owner/repo"
    provider.id_mr = 42
    mr = SimpleNamespace(
        head_pipeline=head_pipeline,
        diff_refs={"head_sha": head_sha},
    )
    if pipelines is not None:
        mr.pipelines = SimpleNamespace(list=lambda *args, **kwargs: pipelines)
    provider.mr = mr
    provider.gl = MagicMock()
    return provider


def test_gitlab_ci_status_head_pipeline_passed():
    provider = _make_gitlab_provider(head_pipeline={"status": "success", "sha": "glhead123"})
    assert provider.get_head_ci_status() == "passed"


def test_gitlab_ci_status_head_pipeline_failed():
    for status in ("failed", "canceled", "cancelled"):
        provider = _make_gitlab_provider(head_pipeline={"status": status, "sha": "glhead123"})
        assert provider.get_head_ci_status() == "failed"


def test_gitlab_ci_status_head_pipeline_pending():
    for status in ("running", "pending", "preparing", "created", "manual", "scheduled"):
        provider = _make_gitlab_provider(head_pipeline={"status": status, "sha": "glhead123"})
        assert provider.get_head_ci_status() == "pending"


def test_gitlab_ci_status_head_pipeline_skipped():
    provider = _make_gitlab_provider(head_pipeline={"status": "skipped", "sha": "glhead123"})
    assert provider.get_head_ci_status() == "passed"


def test_gitlab_ci_status_none_when_no_pipeline():
    provider = _make_gitlab_provider(head_pipeline=None, pipelines=[])
    assert provider.get_head_ci_status() == "none"


def test_gitlab_ci_status_fallback_to_pipelines_list():
    provider = _make_gitlab_provider(
        head_pipeline=None,
        pipelines=[{"status": "success", "sha": "glhead123"}],
    )
    assert provider.get_head_ci_status() == "passed"


def test_gitlab_ci_status_none_when_pipeline_list_fails():
    provider = _make_gitlab_provider(head_pipeline=None)
    provider.mr.pipelines = SimpleNamespace(list=MagicMock(side_effect=RuntimeError("boom")))
    assert provider.get_head_ci_status() == "none"


# ---------------------------------------------------------------------------
# 7. GitLabProvider.is_head_stale_against_base
# ---------------------------------------------------------------------------

def test_gitlab_staleness_returns_false_when_target_branch_unrelated():
    now_iso = "2026-01-01T12:00:00Z"
    provider = _make_gitlab_provider(head_pipeline={"status": "success", "created_at": now_iso})
    provider.mr.target_branch = "main"
    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]

    proj_mock = MagicMock()
    commit_unrelated = SimpleNamespace(
        committed_date="2026-01-01T12:15:00Z",
        diff=lambda: [{"new_path": "docs/readme.md", "old_path": "docs/readme.md"}],
    )
    proj_mock.commits.list.return_value = [commit_unrelated]
    provider.gl.projects.get.return_value = proj_mock

    assert provider.is_head_stale_against_base() is False


def test_gitlab_staleness_returns_true_when_target_branch_touches_pr_file():
    now_iso = "2026-01-01T12:00:00Z"
    provider = _make_gitlab_provider(head_pipeline={"status": "success", "created_at": now_iso})
    provider.mr.target_branch = "main"
    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]

    proj_mock = MagicMock()
    commit_stale = SimpleNamespace(
        committed_date="2026-01-01T12:15:00Z",
        diff=lambda: [{"new_path": "app/main.py", "old_path": "app/main.py"}],
    )
    proj_mock.commits.list.return_value = [commit_stale]
    provider.gl.projects.get.return_value = proj_mock

    assert provider.is_head_stale_against_base() is True


def test_gitlab_staleness_none_when_commit_date_lookup_fails():
    provider = _make_gitlab_provider(head_pipeline=None)
    provider.mr.target_branch = "main"
    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]
    provider.get_pr_head_sha = lambda: "glhead123"

    proj_mock = MagicMock()
    proj_mock.commits.get.side_effect = RuntimeError("boom")
    provider.gl.projects.get.return_value = proj_mock

    assert provider.is_head_stale_against_base() is None


def test_gitlab_staleness_none_when_commit_diff_fails():
    now_iso = "2026-01-01T12:00:00Z"
    provider = _make_gitlab_provider(head_pipeline={"status": "success", "created_at": now_iso})
    provider.mr.target_branch = "main"
    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]

    proj_mock = MagicMock()
    commit_stale = SimpleNamespace(
        committed_date="2026-01-01T12:15:00Z",
        diff=MagicMock(side_effect=RuntimeError("boom")),
    )
    proj_mock.commits.list.return_value = [commit_stale]
    provider.gl.projects.get.return_value = proj_mock

    assert provider.is_head_stale_against_base() is None


# ---------------------------------------------------------------------------
# 8. GitLabProvider.are_all_authors_human
# ---------------------------------------------------------------------------

def test_gitlab_authors_human_when_all_humans():
    provider = _make_gitlab_provider()
    provider.mr.author = {"bot": False, "username": "dhruvil"}
    provider.mr.commits = lambda: [
        {"author_name": "Dhruvil", "author_email": "dhruvil@example.com"},
        {"author_name": "Collab", "author_email": "collab@example.com"},
    ]
    assert provider.are_all_authors_human() is True


def test_gitlab_authors_false_when_author_is_bot():
    provider = _make_gitlab_provider()
    provider.mr.author = {"bot": True, "username": "gitlab-bot"}
    provider.mr.commits = lambda: []
    assert provider.are_all_authors_human() is False


def test_gitlab_authors_false_when_commit_author_is_bot():
    provider = _make_gitlab_provider()
    provider.mr.author = {"bot": False, "username": "dhruvil"}
    provider.mr.commits = lambda: [
        {"author_name": "dependabot[bot]", "author_email": "dependabot@gitlab.com"},
    ]
    assert provider.are_all_authors_human() is False
