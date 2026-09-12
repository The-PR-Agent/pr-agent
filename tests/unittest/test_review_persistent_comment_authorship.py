"""The persistent review must survive an identity PR-Agent cannot prove up front.

`persistent_comment` (default true) promises that "every new review request will edit the
previous one". Verifying that PR-Agent authored the comment it is about to edit protects a
comment it did not write; it must not cost the persistent review in deployments where the
identity is merely unresolved:

- a GitHub App running an automatic command publishes no progress comment, so no earlier
  comment has taught the provider its own login;
- a GitHub Actions token cannot call `GET /user`;
- Azure DevOps ships `agent_identity = ""`.

Creating the first review comment overwrites nothing, so an unverified identity is not a
reason to demote it. Editing an existing comment still requires proof of authorship.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github import Auth, GithubException

from pr_agent.algo.review_finding_state import append_review_state, reconcile_review_findings
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.algo.utils import PRReviewHeader, PRReviewIdentity, add_pr_review_identity
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.azuredevops_provider import AzureDevopsProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.tools.pr_reviewer import PRReviewer

PR_URL = "https://github.com/org/repo/pull/1"
HEAD_FILE = "def retry():\n    attempts = 0\n    while attempts < 3:\n        pass\n"
PATCH = "@@ -1,3 +1,4 @@\n def retry():\n     attempts = 0\n+    while attempts < 3:\n+        pass\n"
DIFF = "## File: 'src/app.py'\n\n" + PATCH
STANDALONE_HEADING = "## Standalone PR Review"

REVIEW_YAML = """review:
  estimated_effort_to_review_[1-5]: |
    2
  relevant_tests: |
    No
  key_issues_to_review:
    - relevant_file: |
        src/app.py
      issue_header: |
        Possible Bug
      issue_content: |
        The retry loop never increments `attempts`, so it spins forever.
      start_line: 3
      end_line: 4
  security_concerns: |
    No
"""

# The prompt asks every key issue for a file, but a model that summarises one finding
# without a location is ordinary. The review summary renders such an entry.
REVIEW_YAML_ISSUE_WITHOUT_FILE = """review:
  estimated_effort_to_review_[1-5]: |
    2
  relevant_tests: |
    No
  key_issues_to_review:
    - relevant_file: |
        src/app.py
      issue_header: |
        Possible Bug
      issue_content: |
        The retry loop never increments `attempts`, so it spins forever.
      start_line: 3
      end_line: 4
    - relevant_file: ""
      issue_header: |
        Missing tests
      issue_content: |
        The PR adds retry logic without any test exercising the failure path.
      start_line: 0
      end_line: 0
  security_concerns: |
    No
"""


class FakeAIHandler:
    response = REVIEW_YAML
    main_pr_language = None

    async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
        return self.response, "stop"


class FakeComment:
    _next_id = 1000

    def __init__(self, body, login):
        FakeComment._next_id += 1
        self.id = FakeComment._next_id
        self.body = body
        self.user = SimpleNamespace(login=login, type="Bot")
        self.html_url = f"{PR_URL}#issuecomment-{self.id}"
        self.edits = []
        self.deleted = False

    def edit(self, body):
        self.edits.append(body)
        self.body = body

    def delete(self):
        self.deleted = True


class FakePR:
    def __init__(self, comments, bot_login):
        self.title = "Add retry loop"
        self.body = "Adds a retry loop."
        self.head = SimpleNamespace(ref="feature/retry", sha="abc123")
        self.comments = list(comments)
        self.created = []
        self.bot_login = bot_login

    def get_issue_comments(self):
        return list(self.comments)

    def create_issue_comment(self, body):
        comment = FakeComment(body, self.bot_login)
        self.comments.append(comment)
        self.created.append(comment)
        return comment


def _stub_pr_data(provider, pr):
    provider.pr = pr
    provider.repo = "org/repo"
    provider.pr_num = 1
    provider.pr_url = PR_URL
    provider.last_commit_id = SimpleNamespace(sha="abc123", html_url="https://github.com/org/repo/commit/abc123")
    provider.diff_files = [FilePatchInfo(base_file="def retry():\n    attempts = 0\n", head_file=HEAD_FILE,
                                         patch=PATCH, filename="src/app.py", edit_type=EDIT_TYPE.MODIFIED)]
    provider.get_languages = lambda: {"Python": 100}
    provider.get_files = lambda: ["src/app.py"]
    provider.get_diff_files = lambda: provider.diff_files
    provider.get_num_of_files = lambda: 1
    provider.get_commit_messages = lambda: ""
    provider.get_pr_description = lambda full=True, split_changes_walkthrough=False: ("Adds a retry loop.", [])
    provider.get_pr_branch = lambda: "feature/retry"


def _github_provider(monkeypatch, deployment_type, comments, bot_login, user_login=None):
    """A real GithubProvider whose PyGithub client is faked.

    user_login=None models a token that cannot call `GET /user`, which is what an
    installation token (including the Actions GITHUB_TOKEN) answers with.
    """
    client = MagicMock()
    if user_login is None:
        client.get_user.side_effect = GithubException(403, {"message": "Resource not accessible by integration"}, None)
    else:
        client.get_user.return_value = SimpleNamespace(raw_data={"login": user_login})
    monkeypatch.setattr(GithubProvider, "_get_github_client", lambda self: client)
    provider = GithubProvider(pr_url=None)
    provider.deployment_type = deployment_type
    provider.github_client = client
    _stub_pr_data(provider, FakePR(comments, bot_login))
    return provider


def _wire_reviewer(monkeypatch, provider):
    monkeypatch.setattr("pr_agent.tools.pr_reviewer.get_git_provider_with_context", lambda url: provider)
    monkeypatch.setattr("pr_agent.tools.pr_reviewer.build_repo_context", lambda git_provider: "")
    monkeypatch.setattr("pr_agent.tools.pr_reviewer.get_skills_context", lambda: "")

    async def no_tickets(git_provider, vars):
        return None

    async def direct_model(f, model_type=None, git_provider=None):
        return await f("gpt-4o")

    monkeypatch.setattr("pr_agent.tools.pr_reviewer.extract_and_cache_pr_tickets", no_tickets)
    monkeypatch.setattr("pr_agent.tools.pr_reviewer.get_pr_diff", lambda *args, **kwargs: (DIFF, []))
    monkeypatch.setattr("pr_agent.tools.pr_reviewer.retry_with_fallback_models", direct_model)


@pytest.fixture
def review_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.config, "publish_output", True)
    monkeypatch.setattr(settings.config, "publish_output_progress", True)
    monkeypatch.setattr(settings.config, "git_provider", "github")
    monkeypatch.setattr(settings.config, "is_auto_command", False, raising=False)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_comment", True)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_finding_state", True, raising=False)
    monkeypatch.setattr(settings.pr_reviewer, "final_update_message", False)
    monkeypatch.setattr(settings.pr_reviewer, "enable_review_labels_effort", False)
    monkeypatch.setattr(settings.pr_reviewer, "enable_review_labels_security", False)
    monkeypatch.setattr(settings.pr_reviewer, "inline_key_issues", False, raising=False)
    monkeypatch.setattr(settings.github, "publish_as_check_run", False, raising=False)
    monkeypatch.setattr(settings.azure_devops_server, "agent_identity", "", raising=False)
    monkeypatch.setattr(FakeAIHandler, "response", REVIEW_YAML)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return settings


def _previous_persistent_review(body_text="previous review body"):
    finding = {"path": "src/app.py", "body": "**Possible Issue**\n\nOld wording of the finding.",
               "line_start": 3, "line_end": 4}
    state = reconcile_review_findings(None, [finding], allow_resolution=False, head_sha="0ld5ha").state
    body = (f"{PRReviewHeader.REGULAR.value} 🔍\n\n{PRReviewIdentity.REGULAR.value}\n\n"
            f"<table><tr><td>{body_text}</td></tr></table>")
    return append_review_state(body, state)


async def test_github_app_auto_review_creates_the_persistent_review(monkeypatch, review_settings):
    """An automatic App command has no earlier comment to learn its login from."""
    review_settings.set("CONFIG.IS_AUTO_COMMAND", True)
    provider = _github_provider(monkeypatch, "app", comments=[], bot_login="pr-agent[bot]")
    _wire_reviewer(monkeypatch, provider)

    await PRReviewer(PR_URL, ai_handler=FakeAIHandler).run()

    created = provider.pr.created
    assert len(created) == 1
    body = created[0].body
    assert not body.startswith(STANDALONE_HEADING)
    assert body.startswith(PRReviewHeader.REGULAR.value)
    assert PRReviewIdentity.REGULAR.value in body


async def test_github_actions_rerun_edits_the_previous_review(monkeypatch, review_settings):
    """Under Actions the token cannot resolve itself, but its comment login is known."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    review_settings.set("CONFIG.IS_AUTO_COMMAND", True)
    previous = FakeComment(_previous_persistent_review(), "github-actions[bot]")
    provider = _github_provider(monkeypatch, "user", comments=[previous],
                                bot_login="github-actions[bot]", user_login=None)
    _wire_reviewer(monkeypatch, provider)

    await PRReviewer(PR_URL, ai_handler=FakeAIHandler).run()

    assert [c for c in provider.pr.created if not c.deleted] == []
    assert len(previous.edits) == 1
    assert not previous.body.startswith(STANDALONE_HEADING)


async def test_review_with_a_fileless_key_issue_still_updates_the_persistent_review(monkeypatch, review_settings):
    """One unusable key issue must not discard the lifecycle state of the others."""
    monkeypatch.setattr(FakeAIHandler, "response", REVIEW_YAML_ISSUE_WITHOUT_FILE)
    previous = FakeComment(_previous_persistent_review(), "reviewer-bot")
    provider = _github_provider(monkeypatch, "user", comments=[previous],
                                bot_login="reviewer-bot", user_login="reviewer-bot")
    _wire_reviewer(monkeypatch, provider)

    await PRReviewer(PR_URL, ai_handler=FakeAIHandler).run()

    assert not any(c.body.startswith(STANDALONE_HEADING) for c in provider.pr.created)
    assert len(previous.edits) == 1
    assert "Missing tests" in previous.body


async def test_azure_devops_without_configured_identity_creates_the_persistent_review(
    monkeypatch, review_settings
):
    """`agent_identity` is empty by default and there is no comment to overwrite."""
    monkeypatch.setattr(review_settings.config, "git_provider", "azure")
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    provider.incremental = SimpleNamespace(is_incremental=False)
    provider._threads_cache = None
    calls = {"persistent": [], "plain": []}
    _stub_pr_data(provider, FakePR([], "pr-agent"))
    provider.get_issue_comments = lambda: []
    provider.get_issue_comments_newest_first = lambda: []
    provider.publish_comment = lambda body, is_temporary=False, **kw: (
        calls["plain"].append(body) or SimpleNamespace(id=1))
    provider.publish_persistent_comment = lambda body, *a, **kw: calls["persistent"].append(body)
    provider.publish_persistent_comment_full = lambda body, *a, **kw: (
        calls["persistent"].append(body) or SimpleNamespace(id=2))
    provider.remove_comment = lambda comment: None
    provider.is_supported = lambda capability: True
    _wire_reviewer(monkeypatch, provider)

    await PRReviewer(PR_URL, ai_handler=FakeAIHandler).run()

    assert not any(body.startswith(STANDALONE_HEADING) for body in calls["plain"])
    assert len(calls["persistent"]) == 1


async def test_control_github_pat_rerun_edits_the_previous_review(monkeypatch, review_settings):
    """A resolvable identity keeps the pre-existing behaviour untouched."""
    previous = FakeComment(_previous_persistent_review(), "reviewer-bot")
    provider = _github_provider(monkeypatch, "user", comments=[previous],
                                bot_login="reviewer-bot", user_login="reviewer-bot")
    _wire_reviewer(monkeypatch, provider)

    await PRReviewer(PR_URL, ai_handler=FakeAIHandler).run()

    assert [c for c in provider.pr.created if not c.deleted] == []
    assert len(previous.edits) == 1
    assert PRReviewIdentity.REGULAR.value in previous.body
    assert "pr-agent-review-state:v1" in previous.body


async def test_forged_review_comment_is_never_edited(monkeypatch, review_settings):
    """A comment carrying the identity marker but written by somebody else stays untouched."""
    review_settings.set("CONFIG.IS_AUTO_COMMAND", True)
    forged_body = add_pr_review_identity("forged review", PRReviewIdentity.REGULAR.value)
    forged = FakeComment(forged_body, "impostor")
    provider = _github_provider(monkeypatch, "app", comments=[forged], bot_login="pr-agent[bot]")
    provider.github_client.get_user.side_effect = GithubException(403, {"message": "no"}, None)
    monkeypatch.setattr(GithubProvider, "_resolve_app_login", lambda self: "")
    _wire_reviewer(monkeypatch, provider)

    await PRReviewer(PR_URL, ai_handler=FakeAIHandler).run()

    assert forged.body == forged_body
    assert forged.edits == []
    created = [c.body for c in provider.pr.created if not c.deleted]
    assert len(created) == 1
    assert created[0].startswith(STANDALONE_HEADING)


async def test_unreadable_comments_do_not_create_a_second_review(monkeypatch, review_settings):
    """When the comment list cannot be read, PR-Agent cannot know what it would replace."""
    review_settings.set("CONFIG.IS_AUTO_COMMAND", True)
    provider = _github_provider(monkeypatch, "app", comments=[], bot_login="pr-agent[bot]")
    monkeypatch.setattr(GithubProvider, "_resolve_app_login", lambda self: "")

    def unreadable():
        raise GithubException(502, {"message": "Server Error"}, None)

    provider.pr.get_issue_comments = unreadable
    _wire_reviewer(monkeypatch, provider)

    await PRReviewer(PR_URL, ai_handler=FakeAIHandler).run()

    created = [c.body for c in provider.pr.created if not c.deleted]
    assert len(created) == 1
    assert created[0].startswith(STANDALONE_HEADING)


def test_unusable_key_issues_are_dropped_without_discarding_the_rest():
    data = {"review": {"key_issues_to_review": [
        {"relevant_file": "src/app.py", "issue_header": "Possible Bug",
         "issue_content": "Real finding.", "start_line": 3, "end_line": 4},
        {"relevant_file": "", "issue_header": "Missing tests", "issue_content": "No location."},
        {"relevant_file": "src/app.py", "issue_header": "Empty", "issue_content": ""},
    ]}}

    findings = PRReviewer._review_findings_from_data(data)

    assert [finding["path"] for finding in findings] == ["src/app.py"]
    assert "Real finding." in findings[0]["body"]


def test_missing_key_issue_collection_still_fails_closed():
    assert PRReviewer._review_findings_from_data({"review": {}}) is None
    assert PRReviewer._review_findings_from_data({"review": {"key_issues_to_review": {"a": 1}}}) is None


# --------------------------------------------------------------------------------------
# A failed app-login resolution must not poison the rest of the request.
#
# `github_app` runs several commands against one provider instance. Caching the empty result
# of a timed-out `GET /app` would demote every command after the first, which is the exact
# behaviour this change exists to remove.
# --------------------------------------------------------------------------------------
def test_a_transient_app_login_failure_is_retried(monkeypatch):
    monkeypatch.setattr(GithubProvider, "_get_github_client", lambda self: MagicMock())
    provider = GithubProvider(pr_url=None)
    provider.deployment_type = "app"
    monkeypatch.setattr(get_settings(), "github", SimpleNamespace(
        app_id="1", private_key="key", deployment_type="app"), raising=False)
    attempts = []
    auths = []

    def integration(**kwargs):
        attempts.append(1)
        auths.append(kwargs["auth"])
        if len(attempts) == 1:
            raise RuntimeError("connection reset")
        return SimpleNamespace(get_app=lambda: SimpleNamespace(slug="pr-agent"))

    monkeypatch.setattr("pr_agent.git_providers.github_provider.GithubIntegration", integration)

    assert provider._resolve_app_login() == ""
    assert provider._resolve_app_login() == "pr-agent[bot]"
    assert len(attempts) == 2
    assert all(isinstance(auth, Auth.AppAuth) for auth in auths)


def test_a_resolved_app_login_is_cached(monkeypatch):
    """Control: the successful answer is still resolved once per provider instance."""
    monkeypatch.setattr(GithubProvider, "_get_github_client", lambda self: MagicMock())
    provider = GithubProvider(pr_url=None)
    provider.deployment_type = "app"
    monkeypatch.setattr(get_settings(), "github", SimpleNamespace(
        app_id="1", private_key="key", deployment_type="app"), raising=False)
    attempts = []
    auths = []

    def integration(**kwargs):
        attempts.append(1)
        auths.append(kwargs["auth"])
        return SimpleNamespace(get_app=lambda: SimpleNamespace(slug="pr-agent"))

    monkeypatch.setattr("pr_agent.git_providers.github_provider.GithubIntegration", integration)

    assert provider._resolve_app_login() == "pr-agent[bot]"
    assert provider._resolve_app_login() == "pr-agent[bot]"
    assert len(attempts) == 1
    assert all(isinstance(auth, Auth.AppAuth) for auth in auths)


def test_an_app_that_never_resolves_stays_unproven(monkeypatch):
    """Control: the conservative outcome is unchanged when the JWT route is unavailable."""
    monkeypatch.setattr(GithubProvider, "_get_github_client", lambda self: MagicMock())
    provider = GithubProvider(pr_url=None)
    provider.deployment_type = "app"
    monkeypatch.setattr(get_settings(), "github", SimpleNamespace(
        app_id="1", private_key="key", deployment_type="app"), raising=False)
    monkeypatch.setattr("pr_agent.git_providers.github_provider.GithubIntegration",
                        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("no private key")))

    assert provider._agent_login() == ""
    with pytest.raises(RuntimeError):
        provider.is_comment_authored_by_pr_agent(SimpleNamespace(user=SimpleNamespace(login="someone")))
