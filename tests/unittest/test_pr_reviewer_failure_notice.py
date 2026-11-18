from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.algo.comment_identity import (
    PRCommandNoticeIdentity,
    PRReviewHeader,
    comment_matches_any_identity,
    comment_matches_identity,
    format_failed_commit_marker,
    get_pr_review_comment_identifiers,
)
from pr_agent.algo.run_details import command_failed
from pr_agent.commands.pr_reviewer import PER_COMMIT_MAX_AUTO_ATTEMPTS, PRReviewer
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider, IncrementalPR, PRCommit, commit_patch_fingerprint

FAILURE = PRCommandNoticeIdentity.REVIEW_FAILURE.value
COMMITS = [PRCommit("a" * 40, "first"), PRCommit("b" * 40, "second")]


class _Provider:
    """A provider that keeps its comments, so a run can find, edit and remove them."""

    def __init__(self, editing=True, authorship=True, reply_to_trigger=False):
        self.comments = []
        self.editing = editing
        self.authorship = authorship
        self.reply_to_trigger = reply_to_trigger
        self.fail_review_edit = False
        self.fail_notice_edit = False

    def get_files(self):
        return ["app.py"]

    def get_diff_files(self):
        return []

    def should_publish_review_as_thread(self):
        return False

    def should_reply_to_trigger_comment(self):
        return self.reply_to_trigger

    def supports_review_comment_identity(self):
        return True

    def supports_review_finding_state(self):
        return self.authorship

    def supports_comment_editing(self):
        return self.editing

    def is_supported(self, capability):
        return capability == "get_issue_comments"

    def is_comment_authored_by_pr_agent(self, comment):
        if comment.author is None:
            raise RuntimeError("comment author cannot be verified")
        return comment.author == "agent"

    def get_issue_comments(self):
        return list(self.comments)

    def get_issue_comments_newest_first(self):
        return list(reversed(self.comments))

    def get_latest_commit_url(self):
        return "https://example.test/commit/1"

    def get_comment_url(self, comment):
        return "https://example.test/comment/1"

    def edit_comment(self, comment, body):
        if self.fail_review_edit and comment_matches_any_identity(
                comment.body, get_pr_review_comment_identifiers(full=True, incremental=False)):
            return False
        if self.fail_notice_edit and comment_matches_identity(comment.body, FAILURE):
            if self.fail_notice_edit == "raise":
                raise RuntimeError("edit refused")
            return False
        comment.body = body
        return True

    def remove_comment(self, comment):
        self.comments.remove(comment)

    def publish_comment(self, body, is_temporary=False, **kwargs):
        comment = SimpleNamespace(body=str(body), author="agent", is_temporary=is_temporary)
        self.comments.append(comment)
        return comment

    def publish_persistent_comment(self, pr_comment, **kwargs):
        return self.publish_persistent_comment_full(pr_comment, **kwargs)

    def publish_persistent_comment_full(self, pr_comment, **kwargs):
        return GitProvider.publish_persistent_comment_full(self, pr_comment, **kwargs)

    def bodies(self):
        return [comment.body for comment in self.comments]

    def notices(self):
        return [comment for comment in self.comments if comment_matches_identity(comment.body, FAILURE)]


@pytest.fixture
def settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setitem(settings.config, "publish_output", True)
    monkeypatch.setitem(settings.config, "is_auto_command", False)
    monkeypatch.setitem(settings.config, "propagate_command_errors", False)
    monkeypatch.setitem(settings.pr_reviewer, "per_commit_review", False)
    monkeypatch.setitem(settings.pr_reviewer, "persistent_comment", True)
    monkeypatch.setitem(settings.pr_reviewer, "persistent_finding_state", True)
    monkeypatch.setitem(settings.pr_reviewer, "final_update_message", False)
    monkeypatch.setitem(settings.pr_reviewer, "publish_review_failure_comment", True)
    monkeypatch.setitem(settings.pr_reviewer, "publish_error_details", False)
    monkeypatch.setitem(settings.pr_reviewer, "inline_key_issues", False)
    missing = object()
    original_comment_id = settings.get("comment_id", missing)
    settings.set("comment_id", "")
    yield settings
    if original_comment_id is missing:
        settings.unset("comment_id", force=True)
    else:
        settings.set("comment_id", original_comment_id)


def _reviewer(provider):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider
    reviewer.pr_url = "https://example.test/pull/1"
    reviewer.incremental = IncrementalPR(False)
    reviewer.remaining_files_list = []
    reviewer.vars = {}
    reviewer.prediction = None
    reviewer._review_state_result = None
    reviewer._review_state_blocked = False
    reviewer._review_state_block_reason = None
    reviewer._review_state_preserved = False
    reviewer._prepare_prediction = AsyncMock()
    review_body = f"{PRReviewHeader.REGULAR.value} {chr(0x1F50D)}\n\nreview output"
    reviewer._prepare_pr_review = MagicMock(return_value=review_body)
    reviewer._should_publish_review_no_suggestions = lambda _review: True
    return reviewer


async def _run(monkeypatch, reviewer, error=None, prediction="prediction", failed_chunks=0):
    async def fake_extract_tickets(git_provider, vars):
        return None

    async def fake_retry(prepare_fn, model_type=None, git_provider=None):
        if error is not None:
            raise error
        reviewer.prediction = prediction
        reviewer.review_failed_chunk_count = failed_chunks

    monkeypatch.setattr("pr_agent.commands.pr_reviewer.extract_and_cache_pr_tickets", fake_extract_tickets)
    monkeypatch.setattr("pr_agent.commands.pr_reviewer.retry_with_fallback_models", fake_retry)
    await reviewer.run()


@pytest.mark.asyncio
async def test_repeated_failures_keep_one_notice(settings, monkeypatch):
    monkeypatch.setitem(settings.pr_reviewer, "publish_error_details", True)
    provider = _Provider()
    reviewer = _reviewer(provider)

    await _run(monkeypatch, reviewer, error=RuntimeError("rate limit reached"))
    first = provider.comments[0]
    await _run(monkeypatch, reviewer, error=RuntimeError("Request timed out"))

    assert provider.comments == [first]
    assert first.body.startswith("Failed to review PR\n\n")
    assert comment_matches_identity(first.body, FAILURE)
    assert "timed out" in first.body and "rate-limited" not in first.body
    assert command_failed() is True


@pytest.mark.asyncio
async def test_failure_notice_is_not_a_review_comment(settings, monkeypatch):
    provider = _Provider()

    await _run(monkeypatch, _reviewer(provider), error=RuntimeError("api down"))

    review_identifiers = get_pr_review_comment_identifiers(full=True, incremental=True)
    assert len(provider.comments) == 1
    assert not comment_matches_any_identity(provider.comments[0].body, review_identifiers)
    assert _reviewer(provider)._persistent_review_comment_exists() is False


@pytest.mark.asyncio
async def test_completed_review_removes_every_notice_the_agent_wrote(settings, monkeypatch):
    provider = _Provider()
    provider.comments = [
        SimpleNamespace(body=f"Failed to review PR\n\n{FAILURE}", author="agent"),
        SimpleNamespace(body=f"Failed to review PR\n\n{FAILURE}", author="someone"),
        SimpleNamespace(body="Failed to review PR", author="agent"),
        SimpleNamespace(body="Failed to review PR\r\n\r\n**Reason:** The model provider timed out.", author="agent"),
        SimpleNamespace(body="Failed to review PR", author="someone"),
        SimpleNamespace(body="Failed to review PRs in this repository so far.", author="agent"),
        SimpleNamespace(body="Failed to review PR\n\nThat heading means the review did not run.", author="agent"),
        SimpleNamespace(
            body="Failed to review PR\n\nThe changes of these commits could not be loaded: `aaaaaaa`", author="agent"),
        SimpleNamespace(body=f"Failed to review PR\n\n{FAILURE}", author="agent"),
    ]
    kept = [provider.comments[index] for index in (1, 4, 5, 6)]

    await _run(monkeypatch, _reviewer(provider))

    assert provider.comments[:4] == kept
    assert len(provider.comments) == 5
    assert "review output" in provider.comments[4].body
    assert command_failed() is False


@pytest.mark.asyncio
async def test_plain_notice_is_updated_only_when_no_notice_has_the_identity(settings, monkeypatch):
    provider = _Provider()
    plain = SimpleNamespace(body="Failed to review PR", author="agent")
    provider.comments = [plain]
    reviewer = _reviewer(provider)

    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))
    assert provider.comments == [plain]
    assert comment_matches_identity(plain.body, FAILURE)

    newer_plain = SimpleNamespace(body="Failed to review PR", author="agent")
    provider.comments.append(newer_plain)
    monkeypatch.setitem(settings.pr_reviewer, "publish_error_details", True)
    await _run(monkeypatch, reviewer, error=RuntimeError("Request timed out"))

    assert provider.comments == [plain, newer_plain]
    assert "timed out" in plain.body
    assert newer_plain.body == "Failed to review PR"


@pytest.mark.asyncio
async def test_review_that_publishes_nothing_still_removes_the_notice(settings, monkeypatch):
    provider = _Provider()
    reviewer = _reviewer(provider)
    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))
    reviewer._should_publish_review_no_suggestions = lambda _review: False

    await _run(monkeypatch, reviewer)

    assert provider.comments == []


@pytest.mark.asyncio
async def test_review_with_failed_chunks_keeps_the_notice_until_a_full_review(settings, monkeypatch):
    provider = _Provider()
    reviewer = _reviewer(provider)
    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))

    await _run(monkeypatch, reviewer, failed_chunks=1)
    assert len(provider.notices()) == 1
    assert any("review output" in body for body in provider.bodies())

    await _run(monkeypatch, reviewer)
    assert provider.notices() == []


@pytest.mark.asyncio
async def test_run_that_ends_without_a_review_keeps_the_notice(settings, monkeypatch):
    provider = _Provider()
    reviewer = _reviewer(provider)
    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))

    await _run(monkeypatch, reviewer, prediction=None)

    assert len(provider.notices()) == 1
    assert command_failed() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("editing", "authorship"), [(False, True), (True, False)])
async def test_provider_that_cannot_track_notices_publishes_plain_comments(
        settings, monkeypatch, editing, authorship):
    provider = _Provider(editing=editing, authorship=authorship)
    reviewer = _reviewer(provider)

    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))
    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))
    assert provider.bodies() == ["Failed to review PR", "Failed to review PR"]

    await _run(monkeypatch, reviewer)
    assert provider.bodies()[:2] == ["Failed to review PR", "Failed to review PR"]


@pytest.mark.asyncio
async def test_replies_to_the_trigger_comment_stay_separate_until_a_review_completes(settings, monkeypatch):
    settings.set("comment_id", "discussion-1")
    provider = _Provider(reply_to_trigger=True)
    reviewer = _reviewer(provider)

    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))
    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))
    assert len(provider.notices()) == 2

    await _run(monkeypatch, reviewer)
    assert provider.notices() == []


@pytest.mark.asyncio
async def test_run_without_a_trigger_comment_to_reply_to_keeps_one_notice(settings, monkeypatch):
    provider = _Provider(reply_to_trigger=True)
    reviewer = _reviewer(provider)

    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))
    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))

    assert len(provider.comments) == 1


@pytest.mark.asyncio
async def test_newest_notice_is_updated_when_an_older_comment_cannot_be_verified(settings, monkeypatch):
    provider = _Provider()
    provider.comments = [SimpleNamespace(body=f"Failed to review PR\n\n{FAILURE}", author=None)]
    reviewer = _reviewer(provider)
    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))
    assert len(provider.comments) == 2

    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))

    assert len(provider.comments) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("unverifiable_first", [True, False])
async def test_completed_review_removes_notices_around_a_comment_that_cannot_be_verified(
        settings, monkeypatch, unverifiable_first):
    provider = _Provider()
    unverifiable = SimpleNamespace(body=f"Failed to review PR\n\n{FAILURE}", author=None)
    notices = [SimpleNamespace(body=f"Failed to review PR\n\n{FAILURE}", author="agent") for _ in range(2)]
    provider.comments = [unverifiable, *notices] if unverifiable_first else [notices[0], unverifiable, notices[1]]

    await _run(monkeypatch, _reviewer(provider))

    assert provider.notices() == [unverifiable]
    assert command_failed() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("edit_failure", [True, "raise"])
async def test_notice_that_cannot_be_updated_is_kept_without_a_second_one(settings, monkeypatch, edit_failure):
    provider = _Provider()
    reviewer = _reviewer(provider)
    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))
    provider.fail_notice_edit = edit_failure

    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))

    assert len(provider.comments) == 1
    assert command_failed() is True


@pytest.mark.asyncio
async def test_comment_that_only_quotes_the_identity_is_neither_updated_nor_removed(settings, monkeypatch):
    provider = _Provider()
    answer = SimpleNamespace(body=f"### **Ask** \u2753\n{FAILURE}\n\n### **Answer:**\nIt marks a notice.",
                             author="agent")
    answer_body = answer.body
    provider.comments = [answer]
    reviewer = _reviewer(provider)

    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))
    assert answer.body == answer_body
    assert len(provider.comments) == 2

    await _run(monkeypatch, reviewer)
    assert answer in provider.comments
    assert provider.notices() == [answer]


@pytest.mark.asyncio
async def test_failed_persistent_write_publishes_a_separate_notice_that_is_removed_later(settings, monkeypatch):
    provider = _Provider()
    reviewer = _reviewer(provider)
    await _run(monkeypatch, reviewer)
    review = provider.comments[0]
    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))
    earlier_notice = provider.notices()[0]
    earlier_body = earlier_notice.body

    provider.fail_review_edit = True
    await _run(monkeypatch, reviewer)

    assert command_failed() is True
    assert len(provider.notices()) == 2
    assert earlier_notice.body == earlier_body

    provider.fail_review_edit = False
    await _run(monkeypatch, reviewer)

    assert provider.comments == [review]
    assert command_failed() is False


@pytest.mark.asyncio
async def test_notice_removal_error_does_not_fail_the_review(settings, monkeypatch):
    provider = _Provider()
    reviewer = _reviewer(provider)
    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))
    provider.comments.append(SimpleNamespace(body=f"Failed to review PR\n\n{FAILURE}", author="agent"))
    refused = provider.comments[1]
    remove = provider.remove_comment

    def remove_comment(comment):
        if comment is refused:
            raise RuntimeError("delete refused")
        remove(comment)

    provider.remove_comment = remove_comment

    await _run(monkeypatch, reviewer)

    assert command_failed() is False
    assert provider.notices() == [refused]
    assert any("review output" in body for body in provider.bodies())


@pytest.mark.asyncio
async def test_single_commit_review_leaves_the_notice_to_the_per_commit_run(settings, monkeypatch):
    provider = _Provider()
    reviewer = _reviewer(provider)
    await _run(monkeypatch, reviewer, error=RuntimeError("api down"))
    reviewer.incremental = IncrementalPR(True)
    reviewer._review_commit = COMMITS[0]
    reviewer._review_fingerprint = commit_patch_fingerprint([("a.py", "@@ -1 +1 @@\n+x\n")])

    await _run(monkeypatch, reviewer)

    assert len(provider.notices()) == 1
    assert any("review output" in body for body in provider.bodies())


def _per_commit_provider(failing=()):
    def scope(commit, incremental):
        if commit.sha in failing:
            raise RuntimeError("api down")
        return commit_patch_fingerprint([(commit.sha, "@@ -1 +1 @@\n+x\n")])

    provider = MagicMock()
    provider.get_pr_commits.return_value = list(COMMITS)
    provider.set_commit_review_scope.side_effect = scope
    provider.supports_review_finding_state.return_value = True
    provider.is_supported.return_value = True
    provider.get_issue_comments.return_value = []
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize(("failing", "review_fails", "completes", "failed_chunks", "removed"), [
    ((), False, True, 0, True),
    (("b" * 40,), False, True, 0, False),
    ((), True, True, 0, False),
    ((), False, False, 0, False),
    ((), False, True, 1, False),
])
async def test_per_commit_run_removes_notices_only_when_every_commit_was_reviewed(
        settings, monkeypatch, failing, review_fails, completes, failed_chunks, removed):
    monkeypatch.setitem(settings.pr_reviewer, "per_commit_review", True)
    reviewer = _reviewer(_per_commit_provider(failing))
    reviewer.vars = {"diff": "", "commit_under_review": "", "commit_messages_str": ""}

    async def review():
        if review_fails and reviewer._review_commit.sha == "a" * 40:
            raise RuntimeError("api down")
        reviewer._review_done = completes
        reviewer.review_failed_chunk_count = failed_chunks

    with patch.object(reviewer, "_run_review", side_effect=review), \
            patch.object(reviewer, "_remove_review_failure_notices") as remove:
        await reviewer.run()

    assert remove.called is removed


@pytest.mark.asyncio
async def test_per_commit_run_with_every_commit_already_reviewed_removes_notices(settings, monkeypatch):
    monkeypatch.setitem(settings.pr_reviewer, "per_commit_review", True)
    provider = _per_commit_provider()
    provider.set_commit_review_scope.side_effect = None
    provider.set_commit_review_scope.return_value = None
    reviewer = _reviewer(provider)
    reviewer.vars = {"diff": "", "commit_under_review": "", "commit_messages_str": ""}

    with patch.object(reviewer, "_run_review") as run_review, \
            patch.object(reviewer, "_remove_review_failure_notices") as remove:
        await reviewer.run()

    run_review.assert_not_awaited()
    assert command_failed() is False
    remove.assert_called_once()


@pytest.mark.asyncio
async def test_per_commit_run_keeps_notices_while_a_failed_commit_is_left_to_a_manual_review(settings, monkeypatch):
    monkeypatch.setitem(settings.pr_reviewer, "per_commit_review", True)
    monkeypatch.setitem(settings.config, "is_auto_command", True)
    provider = _per_commit_provider()
    given_up = commit_patch_fingerprint([("a" * 40, "@@ -1 +1 @@\n+x\n")])
    record = "\n".join([PRCommandNoticeIdentity.REVIEWED_COMMITS.value, "",
                        format_failed_commit_marker(given_up, PER_COMMIT_MAX_AUTO_ATTEMPTS)])
    provider.get_issue_comments.return_value = [SimpleNamespace(body=record)]
    reviewer = _reviewer(provider)
    reviewer.vars = {"diff": "", "commit_under_review": "", "commit_messages_str": ""}
    reviewed = []

    async def review():
        reviewed.append(reviewer._review_commit.sha)
        reviewer._review_done = True

    with patch.object(reviewer, "_run_review", side_effect=review), \
            patch.object(reviewer, "_remove_review_failure_notices") as remove:
        await reviewer.run()

    assert reviewed == ["b" * 40]
    assert command_failed() is False
    remove.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("is_auto_command", [True, False])
async def test_cap_fallback_keeps_the_notice_when_a_commit_could_not_be_loaded(settings, monkeypatch, is_auto_command):
    monkeypatch.setitem(settings.pr_reviewer, "per_commit_review", True)
    monkeypatch.setitem(settings.pr_reviewer, "max_commits_per_review", 1)
    monkeypatch.setitem(settings.config, "is_auto_command", is_auto_command)
    commits = [*COMMITS, PRCommit("c" * 40, "third")]
    provider = _Provider()
    provider.comments = [SimpleNamespace(body=f"Failed to review PR\n\n{FAILURE}", author="agent")]
    earlier = provider.comments[0]
    provider.get_pr_commits = lambda: list(commits)
    provider.pr = SimpleNamespace(commits=len(commits))
    provider.get_incremental_commits = lambda incremental: None

    def scope(commit, incremental):
        if commit.sha == "c" * 40:
            raise RuntimeError("api down")
        return commit_patch_fingerprint([(commit.sha, "@@ -1 +1 @@\n+x\n")])

    provider.set_commit_review_scope = scope
    reviewer = _reviewer(provider)
    reviewer.vars = {"diff": "", "commit_under_review": "", "commit_messages_str": ""}

    await _run(monkeypatch, reviewer)

    assert command_failed() is True
    assert any("review output" in body for body in provider.bodies())
    # An automatic run publishes no notice of its own, so the earlier one is what reports the failure.
    assert provider.notices() == [earlier]
    assert ("`ccccccc`" in earlier.body) is not is_auto_command


@pytest.mark.asyncio
async def test_cap_fallback_removes_the_notice_when_every_commit_was_loaded(settings, monkeypatch):
    monkeypatch.setitem(settings.pr_reviewer, "per_commit_review", True)
    monkeypatch.setitem(settings.pr_reviewer, "max_commits_per_review", 1)
    monkeypatch.setitem(settings.config, "is_auto_command", True)
    provider = _Provider()
    provider.comments = [SimpleNamespace(body=f"Failed to review PR\n\n{FAILURE}", author="agent")]
    provider.get_pr_commits = lambda: list(COMMITS)
    provider.pr = SimpleNamespace(commits=len(COMMITS))
    provider.get_incremental_commits = lambda incremental: None
    provider.set_commit_review_scope = lambda commit, incremental: commit_patch_fingerprint(
        [(commit.sha, "@@ -1 +1 @@\n+x\n")])
    reviewer = _reviewer(provider)
    reviewer.vars = {"diff": "", "commit_under_review": "", "commit_messages_str": ""}

    await _run(monkeypatch, reviewer)

    assert command_failed() is False
    assert provider.notices() == []
    assert reviewer._keep_review_failure_notices is False
