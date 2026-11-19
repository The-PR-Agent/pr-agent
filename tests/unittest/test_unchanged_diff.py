from types import SimpleNamespace

import pytest

from pr_agent.algo.comment_identity import PRCommandNoticeIdentity
from pr_agent.algo.types import FilePatchInfo
from pr_agent.algo.unchanged_diff import (
    RunState,
    UnchangedDiffGuard,
    format_unchanged_diff_marker,
    parse_unchanged_diff_comment,
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
    monkeypatch.setitem(get_settings().config, "enable_auto_approval", False)
    monkeypatch.setitem(get_settings().pr_reviewer, "publish_merge_recommendation_status", False)
    monkeypatch.setitem(get_settings().github, "publish_as_check_run", False)
    return get_settings()


def _reviewed(provider, tool="review"):
    guard = UnchangedDiffGuard(tool, provider)
    assert guard.should_skip() is False
    guard.record()


def _push_a_change(provider):
    provider.head = NEW_HEAD
    provider.files = [FilePatchInfo("a", "c", "@@ -1 +1 @@\n-a\n+c\n", "x.py")]


def test_first_run_is_not_skipped_and_records_the_state(enabled):
    provider = FakeProvider()
    _reviewed(provider)

    records, _ = parse_unchanged_diff_comment(provider.record_body())
    assert set(records) == {"review"}
    assert records["review"].head_sha == HEAD
    assert records["review"].target_branch == "main"


def test_same_head_and_target_is_skipped_with_a_visible_notice(enabled):
    provider = FakeProvider()
    _reviewed(provider)

    provider.files = [FilePatchInfo("a", "b", "@@ -5 +5 @@\n x\n-a\n+b\n", "x.py")]  # target moved, context changed
    assert UnchangedDiffGuard("review", provider).should_skip() is True
    assert "<!-- pr-agent:unchanged-diff-skipped:review commit -->" in provider.record_body()


def test_same_diff_on_a_new_head_is_skipped(enabled):
    provider = FakeProvider()
    _reviewed(provider)

    provider.head = NEW_HEAD  # a rebase that changed no content
    assert UnchangedDiffGuard("review", provider).should_skip() is True
    assert "<!-- pr-agent:unchanged-diff-skipped:review diff -->" in provider.record_body()


def test_a_rebase_that_only_moves_the_hunks_is_skipped(enabled):
    provider = FakeProvider()
    _reviewed(provider)

    provider.head = NEW_HEAD  # the target gained lines above the change, so the hunk moved
    provider.files = [FilePatchInfo("a", "b", "@@ -14 +14 @@\n-a\n+b\n", "x.py")]
    assert UnchangedDiffGuard("review", provider).should_skip() is True


def test_a_rename_alone_is_a_new_diff(enabled):
    provider = FakeProvider()
    _reviewed(provider)

    provider.head = NEW_HEAD
    provider.files = [FilePatchInfo("a", "b", "@@ -1 +1 @@\n-a\n+b\n", "x.py", old_filename="old.py")]
    assert UnchangedDiffGuard("review", provider).should_skip() is False


def test_new_commit_with_a_new_diff_runs(enabled):
    provider = FakeProvider()
    _reviewed(provider)

    _push_a_change(provider)
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

    _push_a_change(provider)
    _reviewed(provider, "review")

    body = provider.record_body()
    assert "skipped" not in body
    records, _ = parse_unchanged_diff_comment(body)
    assert records["review"].head_sha == NEW_HEAD
    assert records["describe"].head_sha == HEAD


def test_a_run_clears_a_stale_skip_notice_before_it_starts(enabled):
    provider = FakeProvider()
    _reviewed(provider)
    assert UnchangedDiffGuard("review", provider).should_skip() is True

    _push_a_change(provider)
    assert UnchangedDiffGuard("review", provider).should_skip() is False  # and then the review fails
    assert "skipped" not in provider.record_body()


def test_markers_in_a_comment_pr_agent_did_not_write_are_ignored(enabled):
    provider = FakeProvider()
    _reviewed(provider)
    foreign = provider.comments[0]
    provider.foreign = {id(foreign)}
    foreign_body = foreign.body

    _reviewed(provider)  # not skipped, and records in a comment of its own

    assert foreign.body == foreign_body
    assert len(provider.comments) == 2


@pytest.mark.parametrize("is_auto, cli_mode, skipped",
                         [(True, False, True), (False, True, True), (False, False, False)])
def test_only_automatic_and_cli_runs_are_skipped(enabled, monkeypatch, is_auto, cli_mode, skipped):
    provider = FakeProvider()
    _reviewed(provider)
    monkeypatch.setitem(get_settings().config, "is_auto_command", is_auto)
    monkeypatch.setitem(get_settings().config, "cli_mode", cli_mode)

    assert UnchangedDiffGuard("review", provider).should_skip() is skipped


def test_setting_off_writes_nothing(enabled, monkeypatch):
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
    assert parse_unchanged_diff_comment(body)[0] == {"review": state}


def test_another_tools_skip_notice_is_dropped_once_the_pr_changed(enabled):
    provider = FakeProvider()
    _reviewed(provider, "review")
    _reviewed(provider, "describe")
    assert UnchangedDiffGuard("describe", provider).should_skip() is True

    _push_a_change(provider)
    _reviewed(provider, "review")

    assert "unchanged-diff-skipped:describe" not in provider.record_body()


def test_another_tools_skip_notice_stays_while_it_still_applies(enabled):
    provider = FakeProvider()
    _reviewed(provider, "review")
    _reviewed(provider, "describe")
    assert UnchangedDiffGuard("describe", provider).should_skip() is True

    assert UnchangedDiffGuard("review", provider).should_skip() is True

    assert "unchanged-diff-skipped:describe" in provider.record_body()


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


@pytest.mark.parametrize("section, key, tools", [
    ("config", "enable_auto_approval", {"review"}),
    ("pr_reviewer", "publish_merge_recommendation_status", {"review"}),
    ("github", "publish_as_check_run", {"review", "describe"}),
])
def test_output_tied_to_the_head_commit_is_never_skipped(enabled, monkeypatch, section, key, tools):
    # A skip would leave the approval, verdict status or check run on the previous head.
    provider = FakeProvider()
    _reviewed(provider, "review")
    _reviewed(provider, "describe")

    monkeypatch.setitem(enabled.get(section), key, True)
    for tool in ("review", "describe"):
        assert UnchangedDiffGuard(tool, provider).should_skip() is (tool not in tools)


def test_the_setting_is_off_by_default():
    assert get_settings().config.get("skip_unchanged_diff") is False


@pytest.mark.parametrize("setting", ["publish_output", "html_markers"])
def test_no_skip_and_no_record_without_published_output_or_hidden_markers(enabled, monkeypatch, setting):
    provider = FakeProvider()
    _reviewed(provider)
    body = provider.record_body()
    if setting == "publish_output":
        monkeypatch.setitem(enabled.config, "publish_output", False)
    else:
        monkeypatch.setattr(provider, "supports_html_comment_markers", lambda: False)

    _reviewed(provider)

    assert provider.record_body() == body


def _skipped_once(provider):
    _reviewed(provider)
    assert UnchangedDiffGuard("review", provider).should_skip() is True
    assert "unchanged-diff-skipped:review" in provider.record_body()


def _typed(monkeypatch, enabled):
    monkeypatch.setitem(enabled.config, "is_auto_command", False)
    monkeypatch.setitem(enabled.config, "cli_mode", False)


def test_a_typed_run_never_skips_even_when_the_record_matches(enabled, monkeypatch):
    provider = FakeProvider()
    _reviewed(provider)
    _typed(monkeypatch, enabled)

    assert UnchangedDiffGuard("review", provider).should_skip() is False


def test_a_typed_run_clears_the_skip_notice_but_never_writes_the_record(enabled, monkeypatch):
    # Arguments typed with the command could narrow the review; it must not stand in for the automatic run.
    provider = FakeProvider()
    _skipped_once(provider)
    before, _ = parse_unchanged_diff_comment(provider.record_body())
    _typed(monkeypatch, enabled)
    provider.files = [FilePatchInfo("a", "b", "@@ -1 +1 @@\n x\n-a\n+b\n", "x.py")]  # target moved

    _reviewed(provider)

    assert "unchanged-diff-skipped:review" not in provider.record_body()
    after, _ = parse_unchanged_diff_comment(provider.record_body())
    assert after == before


def test_a_head_commit_setting_turned_on_after_a_skip_clears_the_notice(enabled, monkeypatch):
    provider = FakeProvider()
    _skipped_once(provider)
    monkeypatch.setitem(enabled.config, "enable_auto_approval", True)

    _reviewed(provider)

    assert "unchanged-diff-skipped:review" not in provider.record_body()
