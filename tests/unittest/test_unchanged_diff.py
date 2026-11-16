from types import SimpleNamespace

import pytest

from pr_agent.algo.comment_identity import PRCommandNoticeIdentity
from pr_agent.algo.types import FilePatchInfo
from pr_agent.algo.unchanged_diff import (
    RunState,
    UnchangedDiffGuard,
    format_unchanged_diff_marker,
    parse_unchanged_diff_records,
)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider

HEAD = "a" * 40
NEW_HEAD = "b" * 40


class FakeProvider:
    publish_persistent_comment_full = GitProvider.publish_persistent_comment_full
    _iter_persistent_comments = GitProvider._iter_persistent_comments
    get_issue_comments_newest_first = GitProvider.get_issue_comments_newest_first

    def __init__(self, patch="@@ -1 +1 @@\n-a\n+b\n", head=HEAD, target="main"):
        self.files = [FilePatchInfo("a", "b", patch, "x.py")]
        self.head = head
        self.target = target
        self.comments = []
        self.foreign = set()

    def get_diff_files(self):
        return self.files

    def get_pr_head_sha(self):
        return self.head

    def get_request_policy_metadata(self, required_fields):
        return {"target_branch": self.target}

    def supports_review_finding_state(self):
        return True

    def supports_html_comment_markers(self):
        return True

    def is_supported(self, capability):
        return True

    def get_issue_comments(self):
        return list(self.comments)

    def is_comment_authored_by_pr_agent(self, comment):
        return id(comment) not in self.foreign

    def publish_comment(self, body, is_temporary=False):
        comment = SimpleNamespace(body=body)
        self.comments.append(comment)
        return comment

    def edit_comment(self, comment, body):
        comment.body = body
        return True

    def get_comment_url(self, comment):
        return ""

    def get_latest_commit_url(self):
        return ""

    def record_body(self):
        identity = PRCommandNoticeIdentity.UNCHANGED_DIFF.value
        bodies = [c.body for c in self.comments if c.body.startswith(identity)]
        assert len(bodies) <= 1
        return bodies[0] if bodies else None


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setitem(get_settings().config, "skip_unchanged_diff", True)
    monkeypatch.setitem(get_settings().config, "is_auto_command", True)
    monkeypatch.setitem(get_settings().config, "cli_mode", False)
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    return get_settings()


def _reviewed(provider, tool="review"):
    guard = UnchangedDiffGuard(tool, provider)
    assert guard.should_skip() is False
    guard.record()


def test_first_run_is_not_skipped_and_records_the_state(enabled):
    provider = FakeProvider()
    _reviewed(provider)

    records = parse_unchanged_diff_records(provider.record_body())
    assert set(records) == {"review"}
    assert records["review"].head_sha == HEAD
    assert records["review"].target_branch == "main"


def test_same_head_and_target_is_skipped_with_a_visible_notice(enabled):
    provider = FakeProvider()
    _reviewed(provider)

    provider.files = [FilePatchInfo("a", "b", "@@ -5 +5 @@\n-a\n+b\n", "x.py")]  # target moved, context shifted
    assert UnchangedDiffGuard("review", provider).should_skip() is True
    assert "`/review` skipped" in provider.record_body()
    assert "head commit" in provider.record_body()


def test_same_diff_on_a_new_head_is_skipped(enabled):
    provider = FakeProvider()
    _reviewed(provider)

    provider.head = NEW_HEAD  # a rebase that changed no content
    assert UnchangedDiffGuard("review", provider).should_skip() is True
    assert "diff" in provider.record_body().split("`/review` skipped")[1]


def test_new_commit_with_a_new_diff_runs(enabled):
    provider = FakeProvider()
    _reviewed(provider)

    provider.head = NEW_HEAD
    provider.files = [FilePatchInfo("a", "c", "@@ -1 +1 @@\n-a\n+c\n", "x.py")]
    assert UnchangedDiffGuard("review", provider).should_skip() is False


def test_retargeted_pr_with_the_same_head_runs(enabled):
    provider = FakeProvider()
    _reviewed(provider)

    provider.target = "release"
    provider.files = [FilePatchInfo("a", "c", "@@ -1 +1 @@\n-a\n+c\n", "x.py")]
    assert UnchangedDiffGuard("review", provider).should_skip() is False


def test_each_tool_has_its_own_record(enabled):
    provider = FakeProvider()
    _reviewed(provider, "review")

    assert UnchangedDiffGuard("describe", provider).should_skip() is False


def test_recording_keeps_other_tools_and_clears_the_skip_notice(enabled):
    provider = FakeProvider()
    _reviewed(provider, "review")
    _reviewed(provider, "describe")
    assert UnchangedDiffGuard("review", provider).should_skip() is True

    provider.head = NEW_HEAD
    provider.files = [FilePatchInfo("a", "c", "@@ -1 +1 @@\n-a\n+c\n", "x.py")]
    _reviewed(provider, "review")

    body = provider.record_body()
    assert "skipped" not in body
    records = parse_unchanged_diff_records(body)
    assert records["review"].head_sha == NEW_HEAD
    assert records["describe"].head_sha == HEAD


def test_a_run_clears_a_stale_skip_notice_before_it_starts(enabled):
    provider = FakeProvider()
    _reviewed(provider)
    assert UnchangedDiffGuard("review", provider).should_skip() is True

    provider.head = NEW_HEAD
    provider.files = [FilePatchInfo("a", "c", "@@ -1 +1 @@\n-a\n+c\n", "x.py")]
    assert UnchangedDiffGuard("review", provider).should_skip() is False  # and then the review fails
    assert "skipped" not in provider.record_body()


def test_markers_in_a_comment_pr_agent_did_not_write_are_ignored(enabled):
    provider = FakeProvider()
    _reviewed(provider)
    provider.foreign = {id(c) for c in provider.comments}

    assert UnchangedDiffGuard("review", provider).should_skip() is False


@pytest.mark.parametrize("is_auto, cli_mode, skipped",
                         [(True, False, True), (False, True, True), (False, False, False)])
def test_only_automatic_and_cli_runs_are_skipped(enabled, monkeypatch, is_auto, cli_mode, skipped):
    provider = FakeProvider()
    _reviewed(provider)
    monkeypatch.setitem(get_settings().config, "is_auto_command", is_auto)
    monkeypatch.setitem(get_settings().config, "cli_mode", cli_mode)

    assert UnchangedDiffGuard("review", provider).should_skip() is skipped


def test_off_by_default_and_writes_nothing(enabled, monkeypatch):
    monkeypatch.setitem(get_settings().config, "skip_unchanged_diff", False)
    provider = FakeProvider()
    _reviewed(provider)

    assert provider.comments == []


def test_providers_that_cannot_verify_authorship_never_skip(enabled):
    provider = FakeProvider()
    _reviewed(provider)
    provider.supports_review_finding_state = lambda: False

    assert UnchangedDiffGuard("review", provider).should_skip() is False


def test_a_provider_error_fails_open(enabled):
    provider = FakeProvider()
    _reviewed(provider)

    def broken():
        raise RuntimeError("api down")
    provider.get_issue_comments = broken

    assert UnchangedDiffGuard("review", provider).should_skip() is False


def test_empty_diff_is_never_skipped(enabled):
    provider = FakeProvider()
    provider.files = []
    _reviewed(provider)
    assert provider.comments == []


def test_branch_names_cannot_break_the_marker():
    state = RunState(diff_hash="f" * 64, head_sha=HEAD, target_branch="feat-->x y")
    marker = format_unchanged_diff_marker("review", state)
    assert marker.count("-->") == 1
    body = f"{PRCommandNoticeIdentity.UNCHANGED_DIFF.value}\n{marker}"
    assert parse_unchanged_diff_records(body) == {"review": state}


def test_another_tools_skip_notice_is_dropped_once_the_pr_changed(enabled):
    provider = FakeProvider()
    _reviewed(provider, "review")
    _reviewed(provider, "describe")
    assert UnchangedDiffGuard("describe", provider).should_skip() is True

    provider.head = NEW_HEAD
    provider.files = [FilePatchInfo("a", "c", "@@ -1 +1 @@\n-a\n+c\n", "x.py")]
    _reviewed(provider, "review")

    assert "`/describe` skipped" not in provider.record_body()


def test_another_tools_skip_notice_stays_while_it_still_applies(enabled):
    provider = FakeProvider()
    _reviewed(provider, "review")
    _reviewed(provider, "describe")
    assert UnchangedDiffGuard("describe", provider).should_skip() is True

    assert UnchangedDiffGuard("review", provider).should_skip() is True

    assert "`/describe` skipped" in provider.record_body()


def test_a_file_without_a_patch_disables_the_diff_check(enabled):
    provider = FakeProvider()
    provider.files.append(FilePatchInfo("", "", "", "logo.png"))  # binary, or content the provider could not read
    _reviewed(provider)

    provider.head = NEW_HEAD  # the binary changed; nothing else did
    assert UnchangedDiffGuard("review", provider).should_skip() is False


def test_a_file_without_a_patch_keeps_the_commit_check(enabled):
    provider = FakeProvider()
    provider.files.append(FilePatchInfo("", "", "", "logo.png"))
    _reviewed(provider)

    assert UnchangedDiffGuard("review", provider).should_skip() is True


def test_an_unknown_target_branch_disables_the_commit_check(enabled):
    provider = FakeProvider(target="")
    _reviewed(provider)

    provider.files = [FilePatchInfo("a", "c", "@@ -1 +1 @@\n-a\n+c\n", "x.py")]  # retargeted, so the diff changed
    assert UnchangedDiffGuard("review", provider).should_skip() is False
