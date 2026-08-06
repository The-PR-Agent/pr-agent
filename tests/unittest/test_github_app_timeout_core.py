"""Unit tests for ``pr_agent.servers.utils.DefaultDictWithTimeout`` and a few
helper functions in ``pr_agent.servers.github_app``.

These tests intentionally avoid network, external credentials, and real sleeps.
Time-dependent behavior is exercised by monkeypatching ``time.monotonic`` on the
``pr_agent.servers.utils`` module.
"""

import asyncio
from types import SimpleNamespace

import pytest

from pr_agent.servers import github_app
from pr_agent.servers import utils as servers_utils
from pr_agent.servers.utils import DefaultDictWithTimeout

# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


def _snapshot_ask_diff_hunk(settings):
    """Snapshot the ``ask_diff_hunk`` setting.

    Returns ``(sentinel, original)`` where ``original is sentinel`` indicates
    the key was absent prior to the snapshot.  The sentinel is a fresh object
    so callers can distinguish "absent" from "present-as-None".
    """
    sentinel = object()
    original = settings.get("ask_diff_hunk", sentinel)
    return sentinel, original


def _restore_ask_diff_hunk(settings, original, sentinel):
    """Restore ``ask_diff_hunk`` to the state captured by ``_snapshot_ask_diff_hunk``.

    When the baseline was absent (``original is sentinel``), the key is truly
    removed via ``unset(force=True)`` rather than being set to ``None`` —
    Dynaconf's ``LazySettings`` does not support ``del settings[key]``.
    """
    if original is sentinel:
        settings.unset("ask_diff_hunk", force=True)
    else:
        settings.set("ask_diff_hunk", original)


# ---------------------------------------------------------------------------
# DefaultDictWithTimeout
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_clock(monkeypatch):
    """Replace ``time.monotonic`` in pr_agent.servers.utils with a controllable clock."""
    state = {"t": 1_000_000.0}
    monkeypatch.setattr(servers_utils.time, "monotonic", lambda: state["t"])
    return state


def _key_times(d):
    # Access the name-mangled private attribute used for testing internals.
    return d._DefaultDictWithTimeout__key_times


class TestDefaultDictWithTimeout:
    def test_update_key_time_on_get_true_refreshes_access_time(self, fake_clock):
        # Use a large refresh_interval to keep __refresh from interfering.
        d = DefaultDictWithTimeout(
            lambda: 0, ttl=1000, refresh_interval=1000, update_key_time_on_get=True
        )
        d["a"] = 1
        t_set = fake_clock["t"]
        assert _key_times(d)["a"] == t_set

        fake_clock["t"] += 7.5
        _ = d["a"]
        assert _key_times(d)["a"] == t_set + 7.5

    def test_update_key_time_on_get_false_keeps_original_time(self, fake_clock):
        d = DefaultDictWithTimeout(
            lambda: 0, ttl=1000, refresh_interval=1000, update_key_time_on_get=False
        )
        d["a"] = 1
        original = _key_times(d)["a"]

        fake_clock["t"] += 7.5
        _ = d["a"]
        assert _key_times(d)["a"] == original

    def test_setitem_always_updates_key_time(self, fake_clock):
        d = DefaultDictWithTimeout(
            lambda: 0, ttl=1000, refresh_interval=1000, update_key_time_on_get=False
        )
        d["a"] = 1
        first = _key_times(d)["a"]
        fake_clock["t"] += 3
        d["a"] = 2
        assert _key_times(d)["a"] == first + 3

    def test_expires_keys_older_than_ttl_when_refresh_runs(self, fake_clock):
        # ttl < refresh_interval so we can advance past both: the throttle
        # permits __refresh to run, and the stale keys are then evicted.
        d = DefaultDictWithTimeout(
            lambda: 0, ttl=2, refresh_interval=5, update_key_time_on_get=False
        )
        d["a"] = 1
        d["b"] = 2

        # Advance past both the TTL and the refresh interval so the throttled
        # cleanup branch runs on the next access.
        fake_clock["t"] += 10

        # Touching a different (new) key triggers __refresh which should
        # purge stale entries.  defaultdict.__missing__ will route through our
        # __setitem__ for the brand-new key, so it gets a fresh timestamp.
        _ = d["fresh"]

        assert "a" not in d
        assert "b" not in d
        assert "fresh" in d
        assert "a" not in _key_times(d)
        assert "b" not in _key_times(d)

    def test_no_ttl_means_no_expiration(self, fake_clock):
        d = DefaultDictWithTimeout(lambda: 0, ttl=None, refresh_interval=1)
        d["a"] = 1
        fake_clock["t"] += 10_000
        _ = d["a"]
        assert d["a"] == 1
        assert "a" in _key_times(d)

    def test_delitem_removes_key_time(self, fake_clock):
        d = DefaultDictWithTimeout(lambda: 0, ttl=10, refresh_interval=1000)
        d["a"] = 1
        del d["a"]
        assert "a" not in d
        assert "a" not in _key_times(d)

    def test_refresh_runs_after_long_idle_period(self, fake_clock):
        d = DefaultDictWithTimeout(
            lambda: 0, ttl=2, refresh_interval=5, update_key_time_on_get=False
        )
        d["a"] = 1
        # Idle well past both the TTL and the refresh interval.  Accessing any
        # key must trigger __refresh and expire stale entries, even after a
        # long quiet period.
        fake_clock["t"] += 100
        _ = d["fresh"]
        assert "a" not in d


# ---------------------------------------------------------------------------
# handle_line_comments
# ---------------------------------------------------------------------------


class TestHandleLineComments:
    @pytest.fixture(autouse=True)
    def restore_ask_diff_hunk_after_each_test(self):
        from pr_agent.config_loader import get_settings

        settings = get_settings()
        sentinel, original = _snapshot_ask_diff_hunk(settings)
        try:
            yield
        finally:
            _restore_ask_diff_hunk(settings, original, sentinel)

    def _payload(self, **overrides):
        comment = {
            "start_line": 10,
            "line": 14,
            "diff_hunk": "@@ -1,3 +1,4 @@\n+new line",
            "path": "src/file.py",
            "side": "RIGHT",
            "id": 987654,
        }
        comment.update(overrides)
        return {"comment": comment}

    def test_returns_empty_string_for_empty_body(self):
        assert github_app.handle_line_comments({}, "") == ""

    def test_converts_ask_to_ask_line_with_metadata(self):
        body = self._payload()
        result = github_app.handle_line_comments(body, "/ask Why this change?")
        assert result.startswith("/ask_line ")
        assert "--line_start=10" in result
        assert "--line_end=14" in result
        assert "--side=RIGHT" in result
        assert "--file_name=src/file.py" in result
        assert "--comment_id=987654" in result
        assert result.endswith("Why this change?")

    def test_missing_start_line_falls_back_to_line(self):
        body = self._payload(start_line=None)
        result = github_app.handle_line_comments(body, "/ask anything")
        assert "--line_start=14" in result
        assert "--line_end=14" in result

    def test_sets_ask_diff_hunk_in_settings(self):
        from pr_agent.config_loader import get_settings

        settings = get_settings()
        body = self._payload(diff_hunk="DIFF_HUNK_SENTINEL")
        github_app.handle_line_comments(body, "/ask hi")
        assert settings.get("ask_diff_hunk") == "DIFF_HUNK_SENTINEL"

    def test_restore_ask_diff_hunk_missing_baseline_truly_absent(self):
        """The cleanup helper must leave ``ask_diff_hunk`` truly absent (not
        present-as-None) when the key did not exist before the test."""
        from pr_agent.config_loader import get_settings

        settings = get_settings()
        # Snapshot the pre-existing outer state so we don't leak our forced
        # absence to other tests.
        outer_sentinel, outer_original = _snapshot_ask_diff_hunk(settings)
        try:
            # Force a known-absent baseline regardless of leaks from elsewhere.
            settings.unset("ask_diff_hunk", force=True)

            sentinel, original = _snapshot_ask_diff_hunk(settings)
            assert original is sentinel

            # Simulate the test body mutating the setting.
            settings.set("ask_diff_hunk", "HUNK")
            assert settings.get("ask_diff_hunk") == "HUNK"

            _restore_ask_diff_hunk(settings, original, sentinel)

            # Key must be absent, not merely None.
            probe = object()
            assert settings.get("ask_diff_hunk", probe) is probe
            assert "ask_diff_hunk" not in settings
        finally:
            _restore_ask_diff_hunk(settings, outer_original, outer_sentinel)

    def test_restore_ask_diff_hunk_existing_value_is_restored(self):
        """If a non-None baseline value existed, the helper restores it."""
        from pr_agent.config_loader import get_settings

        settings = get_settings()
        outer_sentinel, outer_original = _snapshot_ask_diff_hunk(settings)
        try:
            settings.set("ask_diff_hunk", "BASELINE")

            sentinel, original = _snapshot_ask_diff_hunk(settings)
            assert original == "BASELINE"

            settings.set("ask_diff_hunk", "MUTATED")
            _restore_ask_diff_hunk(settings, original, sentinel)

            assert settings.get("ask_diff_hunk") == "BASELINE"
        finally:
            _restore_ask_diff_hunk(settings, outer_original, outer_sentinel)

    def test_restore_ask_diff_hunk_existing_none_baseline_is_preserved(self):
        """If the baseline value was explicitly ``None``, the helper restores
        ``None`` rather than removing the key."""
        from pr_agent.config_loader import get_settings

        settings = get_settings()
        outer_sentinel, outer_original = _snapshot_ask_diff_hunk(settings)
        try:
            settings.set("ask_diff_hunk", None)
            # Sanity: key is present with value None.
            assert "ask_diff_hunk" in settings
            assert settings.get("ask_diff_hunk") is None

            sentinel, original = _snapshot_ask_diff_hunk(settings)
            assert original is None
            assert original is not sentinel

            settings.set("ask_diff_hunk", "MUTATED")
            _restore_ask_diff_hunk(settings, original, sentinel)

            assert settings.get("ask_diff_hunk") is None
        finally:
            _restore_ask_diff_hunk(settings, outer_original, outer_sentinel)

    def test_non_ask_comment_returned_unchanged(self):
        body = self._payload()
        result = github_app.handle_line_comments(body, "just a comment")
        assert result == "just a comment"


# ---------------------------------------------------------------------------
# _check_pull_request_event
# ---------------------------------------------------------------------------


class TestCheckPullRequestEvent:
    def _pr(self, **overrides):
        pr = {
            "url": "https://api.github.com/repos/o/r/pulls/1",
            "state": "open",
            "draft": False,
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
        }
        pr.update(overrides)
        return pr

    def test_accepts_valid_open_non_draft_pr(self):
        body = {"pull_request": self._pr()}
        log_context = {}
        pr, api_url = github_app._check_pull_request_event("opened", body, log_context)
        assert pr is body["pull_request"]
        assert api_url == "https://api.github.com/repos/o/r/pulls/1"
        assert log_context["api_url"] == api_url

    def test_rejects_missing_pull_request(self):
        assert github_app._check_pull_request_event("opened", {}, {}) == ({}, "")

    def test_rejects_missing_url(self):
        body = {"pull_request": self._pr(url=None)}
        assert github_app._check_pull_request_event("opened", body, {}) == ({}, "")

    def test_rejects_closed_pr(self):
        body = {"pull_request": self._pr(state="closed")}
        assert github_app._check_pull_request_event("opened", body, {}) == ({}, "")

    def test_rejects_draft_pr(self):
        body = {"pull_request": self._pr(draft=True)}
        assert github_app._check_pull_request_event("opened", body, {}) == ({}, "")

    def test_rejects_when_draft_field_missing(self):
        # pull_request.get("draft", True) defaults to True, so a missing draft
        # field is treated as draft and rejected.
        pr = self._pr()
        pr.pop("draft")
        body = {"pull_request": pr}
        assert github_app._check_pull_request_event("opened", body, {}) == ({}, "")

    def test_rejects_synchronize_when_created_equals_updated(self):
        body = {
            "pull_request": self._pr(
                created_at="2024-01-01T00:00:00Z", updated_at="2024-01-01T00:00:00Z"
            )
        }
        assert (
            github_app._check_pull_request_event("synchronize", body, {}) == ({}, "")
        )

    def test_accepts_synchronize_when_timestamps_differ(self):
        body = {"pull_request": self._pr()}
        pr, api_url = github_app._check_pull_request_event("synchronize", body, {})
        assert api_url.endswith("/pulls/1")
        assert pr["state"] == "open"


# ---------------------------------------------------------------------------
# handle_push_trigger_for_new_commits dedupe behavior
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def push_trigger_env(monkeypatch):
    """Set up minimal mocks so handle_push_trigger_for_new_commits can run."""
    # Swap module-level dedupe state with fresh test-local instances so we
    # don't leak entries into other tests and don't depend on prior state.
    fresh_duplicate_push_triggers = DefaultDictWithTimeout(ttl=None)
    fresh_pending_conditions = DefaultDictWithTimeout(
        asyncio.locks.Condition, ttl=None
    )
    monkeypatch.setattr(
        github_app, "_duplicate_push_triggers", fresh_duplicate_push_triggers
    )
    monkeypatch.setattr(
        github_app,
        "_pending_task_duplicate_push_conditions",
        fresh_pending_conditions,
    )

    settings = SimpleNamespace(
        github_app=SimpleNamespace(
            handle_push_trigger=True,
            push_trigger_ignore_merge_commits=False,
            push_trigger_pending_tasks_backlog=False,
        )
    )
    monkeypatch.setattr(github_app, "get_settings", lambda: settings)
    monkeypatch.setattr(github_app, "apply_repo_settings", lambda api_url: None)

    eligible_provider = SimpleNamespace(
        verify_eligibility=lambda *a, **kw: github_app.Eligibility.ELIGIBLE
    )
    monkeypatch.setattr(github_app, "get_identity_provider", lambda: eligible_provider)

    calls = {"count": 0}

    async def fake_perform(*args, **kwargs):
        calls["count"] += 1

    monkeypatch.setattr(github_app, "_perform_auto_commands_github", fake_perform)
    yield calls


def _push_body():
    return {
        "pull_request": {
            "url": "https://api.github.com/repos/o/r/pulls/42",
            "state": "open",
            "draft": False,
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
            "merge_commit_sha": "merge-sha",
        },
        "before": "sha-before",
        "after": "sha-after",
    }


class TestPushTriggerDedupe:
    def test_first_event_runs_perform_and_decrements_counter(self, push_trigger_env):
        body = _push_body()
        api_url = body["pull_request"]["url"]

        asyncio.run(
            github_app.handle_push_trigger_for_new_commits(
                body, "push", "alice", "1", "synchronize", {}, agent=None
            )
        )

        assert push_trigger_env["count"] == 1
        # Counter incremented then decremented back to 0.
        assert github_app._duplicate_push_triggers[api_url] == 0

    def test_skips_when_before_equals_after(self, push_trigger_env):
        body = _push_body()
        body["before"] = body["after"]

        asyncio.run(
            github_app.handle_push_trigger_for_new_commits(
                body, "push", "alice", "1", "synchronize", {}, agent=None
            )
        )

        assert push_trigger_env["count"] == 0

    def test_skips_merge_commit_when_configured(self, push_trigger_env, monkeypatch):
        body = _push_body()
        body["after"] = body["pull_request"]["merge_commit_sha"]
        settings = github_app.get_settings()
        settings.github_app.push_trigger_ignore_merge_commits = True

        asyncio.run(
            github_app.handle_push_trigger_for_new_commits(
                body, "push", "alice", "1", "synchronize", {}, agent=None
            )
        )

        assert push_trigger_env["count"] == 0

    def test_skips_when_handle_push_trigger_disabled(self, push_trigger_env):
        github_app.get_settings().github_app.handle_push_trigger = False

        asyncio.run(
            github_app.handle_push_trigger_for_new_commits(
                _push_body(), "push", "alice", "1", "synchronize", {}, agent=None
            )
        )

        assert push_trigger_env["count"] == 0

    def test_discards_when_max_active_tasks_reached(self, push_trigger_env):
        body = _push_body()
        api_url = body["pull_request"]["url"]
        # Simulate an already-running task with backlog disabled (max=1).
        github_app._duplicate_push_triggers[api_url] = 1

        asyncio.run(
            github_app.handle_push_trigger_for_new_commits(
                body, "push", "alice", "1", "synchronize", {}, agent=None
            )
        )

        # Third path: counter is left untouched, perform never runs.
        assert push_trigger_env["count"] == 0
        assert github_app._duplicate_push_triggers[api_url] == 1

    def test_invalid_pr_event_short_circuits(self, push_trigger_env):
        body = _push_body()
        body["pull_request"]["state"] = "closed"

        asyncio.run(
            github_app.handle_push_trigger_for_new_commits(
                body, "push", "alice", "1", "synchronize", {}, agent=None
            )
        )

        assert push_trigger_env["count"] == 0
