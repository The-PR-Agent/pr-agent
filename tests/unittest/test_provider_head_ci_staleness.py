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
    app_login="pr-agent[bot]",
):
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "owner/repo"
    provider.base_url = "https://api.github.com"
    # Resolved app identity, as `_resolve_app_login()` would return it:
    # setting the cache avoids a live `GET /app` in tests.
    provider._app_login = app_login
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


def test_github_ci_status_counts_spoofed_pr_agent_named_run():
    # A foreign check run that merely reuses one of PR-Agent's names must
    # still count: the app slug, not the name, authenticates PR-Agent's runs.
    runs = [
        SimpleNamespace(status="completed", conclusion="failure", app=None, name="PR Agent - Review"),
        SimpleNamespace(status="completed", conclusion="success", app=None, name="test"),
    ]
    provider = _make_github_provider(check_runs=runs)
    assert provider.get_head_ci_status() == "failed"

    # Same for a run from another app (e.g. a workflow job with that name)
    other_app = SimpleNamespace(slug="github-actions", name="GitHub Actions")
    runs_other_app = [
        SimpleNamespace(status="completed", conclusion="failure", app=other_app, name="PR Agent - Describe"),
    ]
    provider_other_app = _make_github_provider(check_runs=runs_other_app)
    assert provider_other_app.get_head_ci_status() == "failed"


def test_github_ci_status_ignores_self_hosted_app_check_run():
    # A self-hosted app under any slug is recognised by its resolved
    # identity, not by a hard-coded slug list.
    own_app = SimpleNamespace(slug="acme-pr-agent", name="Acme PR Agent")
    runs = [
        SimpleNamespace(status="in_progress", conclusion=None, app=own_app, name="PR Agent - Review"),
        SimpleNamespace(status="completed", conclusion="success", app=None, name="ci/build"),
    ]
    provider = _make_github_provider(check_runs=runs, app_login="acme-pr-agent[bot]")
    assert provider.get_head_ci_status() == "passed"


def test_github_ci_status_counts_pr_agent_slug_under_other_identity():
    # Same slug, different running app: not our run, so it counts.
    other_app = SimpleNamespace(slug="pr-agent", name="PR Agent")
    runs = [
        SimpleNamespace(status="completed", conclusion="failure", app=other_app, name="PR Agent - Review"),
    ]
    provider = _make_github_provider(check_runs=runs, app_login="acme-pr-agent[bot]")
    assert provider.get_head_ci_status() == "failed"


def test_github_ci_status_counts_github_actions_run():
    # The Action's runs share the `github-actions` app with every other
    # workflow, so identity alone never skips them; only the running job
    # is left out (via GITHUB_RUN_ID).
    actions_app = SimpleNamespace(slug="github-actions", name="GitHub Actions")
    runs = [
        SimpleNamespace(status="in_progress", conclusion=None, app=actions_app, name="PR Agent - Review"),
        SimpleNamespace(status="completed", conclusion="success", app=None, name="test"),
    ]
    provider = _make_github_provider(check_runs=runs)
    assert provider.get_head_ci_status() == "pending"


def test_github_ci_status_counts_own_slug_when_identity_unresolvable(monkeypatch):
    # Identity unresolvable: fail closed, do not guess from the slug.
    pr_agent_app = SimpleNamespace(slug="pr-agent", name="PR Agent")
    runs = [
        SimpleNamespace(status="in_progress", conclusion=None, app=pr_agent_app, name="PR Agent - Review"),
        SimpleNamespace(status="completed", conclusion="success", app=None, name="test"),
    ]
    provider = _make_github_provider(check_runs=runs)
    monkeypatch.setattr(provider, "_resolve_app_login", lambda: "")
    assert provider.get_head_ci_status() == "pending"


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


def test_github_staleness_falls_back_to_head_push_time_when_no_ci_runs():
    now = datetime.now(UTC)
    provider = _make_github_provider(check_runs=[])
    # The head commit is forward-dated by a day: that date must NOT stand in
    # for the push time, or the head would look fresh.
    head_commit = provider.repo_obj.get_commit.return_value
    head_commit.commit = SimpleNamespace(committer=SimpleNamespace(date=now + timedelta(days=1)))
    # The push actually happened at `now`.
    provider.repo_obj.get_events.return_value = [
        SimpleNamespace(
            type="PushEvent",
            payload={"head": "head123"},
            created_at=now,
        )
    ]

    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]
    provider.pr.base = SimpleNamespace(ref="main")

    base_commit = SimpleNamespace(
        commit=SimpleNamespace(committer=SimpleNamespace(date=now + timedelta(minutes=5))),
        files=[SimpleNamespace(filename="app/main.py")],
    )
    provider.repo_obj.get_commits.return_value = [base_commit]
    assert provider.is_head_stale_against_base() is True


def test_github_staleness_returns_none_when_push_time_unknown():
    # No CI runs and no push event for the head: fail closed, not fresh.
    provider = _make_github_provider(check_runs=[])
    provider.repo_obj.get_events.return_value = []

    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]
    provider.pr.base = SimpleNamespace(ref="main")
    provider.repo_obj.get_commits.return_value = []
    assert provider.is_head_stale_against_base() is None


def test_github_staleness_counts_spoofed_pr_agent_named_run():
    # A foreign run reusing a PR-Agent check name is still real CI: its
    # completion time sets the reference timestamp.
    now = datetime.now(UTC)
    runs = [
        SimpleNamespace(
            status="completed",
            conclusion="success",
            app=None,
            completed_at=now + timedelta(hours=1),
            name="PR Agent - Review",
        )
    ]
    provider = _make_github_provider(check_runs=runs)
    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]
    provider.pr.base = SimpleNamespace(ref="main")

    # Base moved 30 minutes after the push, but CI ran an hour after it,
    # so the head's CI is current and the head is not stale.
    base_commit = SimpleNamespace(
        commit=SimpleNamespace(committer=SimpleNamespace(date=now + timedelta(minutes=30))),
        files=[SimpleNamespace(filename="app/main.py")],
    )
    provider.repo_obj.get_commits.return_value = [base_commit]
    assert provider.is_head_stale_against_base() is False


def test_github_staleness_skips_own_run_by_identity():
    # The only completed run is PR-Agent's own (self-hosted slug, matching
    # identity): it must not set the reference timestamp, so with no push
    # event either, staleness is unknown rather than fresh.
    now = datetime.now(UTC)
    own_app = SimpleNamespace(slug="acme-pr-agent", name="Acme PR Agent")
    runs = [
        SimpleNamespace(
            status="completed",
            conclusion="success",
            app=own_app,
            completed_at=now,
            name="PR Agent - Review",
        )
    ]
    provider = _make_github_provider(check_runs=runs, app_login="acme-pr-agent[bot]")
    provider.repo_obj.get_events.return_value = []
    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]
    provider.pr.base = SimpleNamespace(ref="main")
    provider.repo_obj.get_commits.return_value = []
    assert provider.is_head_stale_against_base() is None


def test_github_staleness_reads_push_time_from_fork_events():
    # Fork PR: the push event lives in the fork's stream, not the base
    # repo's, so the fork's events must be read.
    now = datetime.now(UTC)
    provider = _make_github_provider(check_runs=[])
    provider.repo_obj.full_name = "owner/repo"
    provider.repo_obj.get_events.return_value = []
    provider.pr.head.repo = SimpleNamespace(
        full_name="contributor/repo",
        get_events=lambda: [
            SimpleNamespace(type="PushEvent", payload={"head": "head123"}, created_at=now),
        ],
    )

    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]
    provider.pr.base = SimpleNamespace(ref="main")

    base_commit = SimpleNamespace(
        commit=SimpleNamespace(committer=SimpleNamespace(date=now + timedelta(minutes=5))),
        files=[SimpleNamespace(filename="app/main.py")],
    )
    provider.repo_obj.get_commits.return_value = [base_commit]
    assert provider.is_head_stale_against_base() is True


def test_github_staleness_uses_base_events_for_same_repo_pr():
    # Same-repo PR: the head repo is the base repo, so its events are used
    # even though the head carries a repo object.
    now = datetime.now(UTC)
    provider = _make_github_provider(check_runs=[])
    provider.repo_obj.full_name = "owner/repo"
    provider.repo_obj.get_events.return_value = [
        SimpleNamespace(type="PushEvent", payload={"head": "head123"}, created_at=now),
    ]
    provider.pr.head.repo = SimpleNamespace(full_name="owner/repo", get_events=lambda: [])

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


# ---------------------------------------------------------------------------
# 7. GitLabProvider.is_head_stale_against_base
# ---------------------------------------------------------------------------

def _gitlab_failed_pipeline_with_jobs(status, jobs):
    provider = _make_gitlab_provider(head_pipeline={"status": status, "sha": "glhead123", "id": 17})
    pipeline = provider.gl.projects.get.return_value.pipelines.get.return_value
    pipeline.jobs.list.return_value = [SimpleNamespace(name=name, status=job) for name, job in jobs]
    return provider


def test_gitlab_ci_status_ignores_its_failed_verdict_job():
    provider = _gitlab_failed_pipeline_with_jobs("failed", [("PR Agent - Review", "failed"), ("test", "success")])
    assert provider.get_head_ci_status() == "passed"


def test_gitlab_ci_status_keeps_other_pipeline_failures():
    provider = _gitlab_failed_pipeline_with_jobs("failed", [("PR Agent - Review", "failed"), ("test", "failed")])
    assert provider.get_head_ci_status() == "failed"


def test_gitlab_ci_status_keeps_unexplained_pipeline_failures():
    for status in ("failed", "canceled"):
        provider = _gitlab_failed_pipeline_with_jobs(status, [("test", "success")])
        assert provider.get_head_ci_status() == "failed"


def test_gitlab_staleness_is_unknown_without_a_pipeline_timestamp():
    provider = _make_gitlab_provider(head_pipeline=None)
    provider.mr.target_branch = "main"
    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]
    project = MagicMock()
    provider.gl.projects.get.return_value = project

    assert provider.is_head_stale_against_base() is None
    project.commits.get.assert_not_called()
    project.commits.list.assert_not_called()


def test_gitlab_staleness_returns_false_when_target_branch_unrelated():
    now_iso = "2026-01-01T12:00:00Z"
    provider = _make_gitlab_provider(head_pipeline={"status": "success", "created_at": now_iso})
    provider.mr.target_branch = "main"
    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]

    proj_mock = MagicMock()
    commit_unrelated = SimpleNamespace(
        committed_date="2026-01-01T12:15:00Z",
        diff=lambda **kwargs: [{"new_path": "docs/readme.md", "old_path": "docs/readme.md"}],
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
        diff=lambda **kwargs: [{"new_path": "app/main.py", "old_path": "app/main.py"}],
    )
    proj_mock.commits.list.return_value = [commit_stale]
    provider.gl.projects.get.return_value = proj_mock

    assert provider.is_head_stale_against_base() is True


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


def test_github_ci_status_evaluates_user_check_named_pr_agent_tests():
    # User's test named "pr-agent tests" must NOT be skipped as PR-Agent's own run
    runs = [
        SimpleNamespace(status="completed", conclusion="failure", app=None, name="pr-agent tests"),
    ]
    provider = _make_github_provider(check_runs=runs)
    assert provider.get_head_ci_status() == "failed"


def test_github_ci_status_ignores_running_action_job(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "123456789")
    runs = [
        # Action's own job in progress should be ignored via GITHUB_RUN_ID in details_url/html_url
        SimpleNamespace(
            status="in_progress",
            conclusion=None,
            app=None,
            name="Run PR Agent Action",
            details_url="https://github.com/org/repo/actions/runs/123456789/job/456",
        ),
        SimpleNamespace(status="completed", conclusion="success", app=None, name="test"),
    ]
    provider = _make_github_provider(check_runs=runs)
    assert provider.get_head_ci_status() == "passed"


def test_github_staleness_detects_renamed_file_previous_filename():
    now = datetime.now(UTC)
    runs = [SimpleNamespace(status="completed", conclusion="success", app=None, completed_at=now, name="test")]
    provider = _make_github_provider(check_runs=runs)
    provider.get_diff_files = lambda: [SimpleNamespace(filename="new_path.py", old_filename="old_path.py")]
    provider.pr.base = SimpleNamespace(ref="main")

    base_commit = SimpleNamespace(
        commit=SimpleNamespace(committer=SimpleNamespace(date=now + timedelta(minutes=5))),
        files=[SimpleNamespace(filename="old_path.py")],
    )
    provider.repo_obj.get_commits.return_value = [base_commit]
    assert provider.is_head_stale_against_base() is True


def test_github_staleness_fails_closed_on_51_commits():
    now = datetime.now(UTC)
    runs = [SimpleNamespace(status="completed", conclusion="success", app=None, completed_at=now, name="test")]
    provider = _make_github_provider(check_runs=runs)
    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]
    provider.pr.base = SimpleNamespace(ref="main")

    # 51 unrelated commits should cause pagination limit truncation and return None
    commits = [
        SimpleNamespace(
            commit=SimpleNamespace(committer=SimpleNamespace(date=now + timedelta(minutes=i + 1))),
            files=[SimpleNamespace(filename="other.py")],
        )
        for i in range(51)
    ]
    provider.repo_obj.get_commits.return_value = commits
    assert provider.is_head_stale_against_base() is None


def test_gitlab_staleness_detects_renamed_file_previous_filename():
    provider = _make_gitlab_provider()
    provider.mr.target_branch = "main"
    provider.mr.head_pipeline = {
        "status": "success",
        "sha": "headsha123",
        "created_at": "2026-01-01T12:00:00Z",
    }
    provider.get_diff_files = lambda: [SimpleNamespace(filename="new_path.py", old_filename="old_path.py")]

    proj_mock = MagicMock()
    commit_stale = SimpleNamespace(
        committed_date="2026-01-01T12:15:00Z",
        diff=lambda **kwargs: [{"new_path": "old_path.py", "old_path": "old_path.py"}],
    )
    proj_mock.commits.list.return_value = [commit_stale]
    provider.gl.projects.get.return_value = proj_mock

    assert provider.is_head_stale_against_base() is True


def test_gitlab_staleness_fails_closed_on_diff_read_error():
    provider = _make_gitlab_provider()
    provider.mr.target_branch = "main"
    provider.mr.head_pipeline = {
        "status": "success",
        "sha": "headsha123",
        "created_at": "2026-01-01T12:00:00Z",
    }
    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]

    def failing_diff():
        raise RuntimeError("diff API error")

    proj_mock = MagicMock()
    commit_err = SimpleNamespace(
        committed_date="2026-01-01T12:15:00Z",
        diff=failing_diff,
    )
    proj_mock.commits.list.return_value = [commit_err]
    provider.gl.projects.get.return_value = proj_mock

    assert provider.is_head_stale_against_base() is None


def test_gitlab_staleness_fails_closed_on_50_commits():
    provider = _make_gitlab_provider()
    provider.mr.target_branch = "main"
    provider.mr.head_pipeline = {
        "status": "success",
        "sha": "headsha123",
        "created_at": "2026-01-01T12:00:00Z",
    }
    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]

    # 50 commits returned by per_page=50 means potential truncation
    proj_mock = MagicMock()
    commits_list = [
        SimpleNamespace(
            committed_date="2026-01-01T12:15:00Z",
            diff=lambda **kwargs: [{"new_path": "other.py", "old_path": "other.py"}],
        )
        for _ in range(50)
    ]
    proj_mock.commits.list.return_value = commits_list
    provider.gl.projects.get.return_value = proj_mock

    assert provider.is_head_stale_against_base() is None


def test_github_authors_none_when_pr_commits_exceed_250():
    provider = _make_github_provider()
    provider.pr.user = SimpleNamespace(type="User", login="dhruvil")
    provider.pr.commits = 251
    assert provider.are_all_authors_human() is None


def test_gitlab_authors_none_when_project_access_token_bot():
    provider = _make_gitlab_provider()
    # GitLab project/group access token bot username pattern
    provider.mr.author = {"bot": False, "username": "project_123_bot_456"}
    provider.mr.commits = lambda: []
    assert provider.are_all_authors_human() is None


def test_gitlab_authors_none_when_commit_author_is_project_access_token_bot():
    provider = _make_gitlab_provider()
    provider.mr.author = {"bot": False, "username": "dhruvil"}
    provider.mr.commits = lambda: [
        {"author_name": "project_42_bot_token", "author_email": "project_42_bot@noreply.gitlab.com"},
    ]
    assert provider.are_all_authors_human() is None


def test_gitlab_staleness_passes_get_all_to_diff():
    provider = _make_gitlab_provider()
    provider.mr.target_branch = "main"
    provider.mr.head_pipeline = {
        "status": "success",
        "sha": "headsha123",
        "created_at": "2026-01-01T12:00:00Z",
    }
    provider.get_diff_files = lambda: [SimpleNamespace(filename="app/main.py")]

    diff_mock = MagicMock(return_value=[{"new_path": "other.py", "old_path": "other.py"}])
    commit = SimpleNamespace(
        committed_date="2026-01-01T12:15:00Z",
        diff=diff_mock,
    )
    proj_mock = MagicMock()
    proj_mock.commits.list.return_value = [commit]
    provider.gl.projects.get.return_value = proj_mock

    assert provider.is_head_stale_against_base() is False
    diff_mock.assert_called_once_with(get_all=True, per_page=100)


def test_github_ci_status_evaluates_completed_sibling_job_in_same_run(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    runs = [
        # Action's own job in progress
        SimpleNamespace(
            status="in_progress",
            conclusion=None,
            app=None,
            name="pr-agent-action",
            details_url="https://github.com/org/repo/actions/runs/123/job/1",
        ),
        # Completed sibling job in the same run that failed
        SimpleNamespace(
            status="completed",
            conclusion="failure",
            app=None,
            name="tests",
            details_url="https://github.com/org/repo/actions/runs/123/job/2",
        ),
    ]
    provider = _make_github_provider(check_runs=runs)
    assert provider.get_head_ci_status() == "failed"


def test_github_ci_status_does_not_prefix_match_run_id(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "55")
    runs = [
        # Run 555 should not match run 55
        SimpleNamespace(
            status="completed",
            conclusion="success",
            app=None,
            name="other-run-job",
            details_url="https://github.com/org/repo/actions/runs/555/job/1",
        ),
    ]
    provider = _make_github_provider(check_runs=runs)
    assert provider.get_head_ci_status() == "passed"


def test_github_ci_status_fails_closed_on_check_runs_error():
    provider = _make_github_provider()
    commit = provider.repo_obj.get_commit.return_value
    commit.get_check_runs.side_effect = GithubException(403, "Rate limit", {})
    assert provider.get_head_ci_status() is None


def test_github_ci_status_fails_closed_on_combined_status_error():
    provider = _make_github_provider(check_runs=[])
    commit = provider.repo_obj.get_commit.return_value
    commit.get_combined_status.side_effect = GithubException(403, "Rate limit", {})
    assert provider.get_head_ci_status() is None


def test_github_staleness_detects_filtered_file_moved_by_base():
    now = datetime.now(UTC)
    runs = [SimpleNamespace(status="completed", conclusion="success", app=None, completed_at=now, name="test")]
    provider = _make_github_provider(check_runs=runs)
    provider.get_diff_files = lambda: []
    provider.get_filtered_diff_file_names = lambda: ["uv.lock"]
    provider.pr.base = SimpleNamespace(ref="main")

    base_commit = SimpleNamespace(
        commit=SimpleNamespace(committer=SimpleNamespace(date=now + timedelta(minutes=5))),
        files=[SimpleNamespace(filename="uv.lock")],
    )
    provider.repo_obj.get_commits.return_value = [base_commit]
    assert provider.is_head_stale_against_base() is True


def test_gitlab_staleness_detects_filtered_file_moved_by_base():
    now_iso = "2026-01-01T12:00:00Z"
    provider = _make_gitlab_provider(head_pipeline={"status": "success", "created_at": now_iso})
    provider.mr.target_branch = "main"
    provider.get_diff_files = lambda: []
    provider.get_filtered_diff_file_names = lambda: ["uv.lock"]

    proj_mock = MagicMock()
    commit_stale = SimpleNamespace(
        committed_date="2026-01-01T12:15:00Z",
        diff=lambda **kwargs: [{"new_path": "uv.lock", "old_path": "uv.lock"}],
    )
    proj_mock.commits.list.return_value = [commit_stale]
    provider.gl.projects.get.return_value = proj_mock

    assert provider.is_head_stale_against_base() is True


# ---------------------------------------------------------------------------
# 9. GithubProvider._upsert_check_run external_id stamping
# ---------------------------------------------------------------------------

class _CapturingRequester:
    def __init__(self):
        self.calls = []

    def requestJsonAndCheck(self, method, url, input=None, **kwargs):
        self.calls.append({"method": method, "url": url, "input": input})
        return {}, {"id": 99}


def _make_check_run_provider(requester):
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "owner/repo"
    provider.base_url = "https://api.github.com"
    provider.last_commit_id = SimpleNamespace(sha="head123")
    provider._check_run_ids = {}
    provider.pr = SimpleNamespace(_requester=requester)
    provider._find_existing_check_run = lambda *args, **kwargs: None
    return provider


def test_upsert_check_run_stamps_workflow_run_id(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "123456789")
    requester = _CapturingRequester()
    provider = _make_check_run_provider(requester)
    assert provider._upsert_check_run("review", {"status": "in_progress"}) is True
    assert len(requester.calls) == 1
    created = requester.calls[0]["input"]
    assert created["name"] == "PR Agent - Review"
    assert created["external_id"] == "123456789"


def test_upsert_check_run_no_external_id_without_run_id(monkeypatch):
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    requester = _CapturingRequester()
    provider = _make_check_run_provider(requester)
    assert provider._upsert_check_run("review", {"status": "in_progress"}) is True
    assert "external_id" not in requester.calls[0]["input"]
