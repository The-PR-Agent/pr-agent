"""Suppress overlapping deliveries and cache only eligible completed GitLab work."""

import asyncio
import copy
from contextlib import asynccontextmanager
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from starlette_context import request_cycle_context

from pr_agent.agent.request_policy import RequestOutcome
from pr_agent.algo.run_details import get_run_details, record_command_failure
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.config_security import is_repo_host_only_key
from pr_agent.servers import gitlab_webhook, lambda_handoff
from pr_agent.servers.utils import DefaultDictWithTimeout

MR_URL = "https://gitlab.example.com/group/project/-/merge_requests/39"
REAL_REVIEWER_CHECK = gitlab_webhook.is_bot_assigned_as_reviewer
REAL_REVOKE_APPROVAL = gitlab_webhook.revoke_auto_approval_on_push
REAL_SHOULD_PROCESS_PR_LOGIC = gitlab_webhook.should_process_pr_logic


@pytest.fixture(autouse=True)
def fresh_completed_deliveries(monkeypatch):
    monkeypatch.setattr(gitlab_webhook, "_completed_webhook_deliveries", DefaultDictWithTimeout(
        float, ttl=300, update_key_time_on_get=False,
    ))


def _reviewer_event(project_id=7):
    return {
        "object_kind": "merge_request",
        "user": {"username": "reviewer", "id": 3},
        "project": {"id": project_id},
        "object_attributes": {"action": "update", "url": MR_URL},
        "changes": {"reviewers": {"previous": [], "current": [{"id": 516}]}},
    }


@pytest.fixture
def reviewer_webhook(monkeypatch):
    monkeypatch.setitem(get_settings().gitlab, "shared_secret", "test-secret")
    monkeypatch.setitem(get_settings().gitlab, "personal_access_token", "fixture-api-token")
    monkeypatch.setitem(get_settings().gitlab, "webhook_delivery_deduplication", True)
    monkeypatch.setitem(get_settings().gitlab, "handle_reviewer_assignment", True)
    monkeypatch.setitem(get_settings().config, "enable_auto_approval", False)
    monkeypatch.setitem(get_settings().pr_reviewer, "publish_merge_recommendation_status", False)
    monkeypatch.setitem(get_settings().push_outputs, "enable", False)
    monkeypatch.setattr(gitlab_webhook, "is_bot_user", lambda _: False)
    monkeypatch.setattr(gitlab_webhook, "should_process_pr_logic", lambda _: True)
    monkeypatch.setattr(gitlab_webhook, "apply_repo_settings", mock.Mock())
    monkeypatch.setattr(gitlab_webhook, "revoke_auto_approval_on_push", mock.Mock())
    monkeypatch.setattr(gitlab_webhook, "is_bot_assigned_as_reviewer", mock.AsyncMock(return_value=True))
    perform = mock.AsyncMock(return_value=True)
    monkeypatch.setattr(gitlab_webhook, "_perform_commands_gitlab", perform)
    return TestClient(gitlab_webhook.app), perform


def _post(client, data, delivery_id, *, header="webhook-id", instance="https://gitlab.example.com"):
    headers = {"X-Gitlab-Token": "test-secret", "X-Gitlab-Instance": instance}
    if delivery_id is not None:
        headers[header] = delivery_id
    response = client.post("/webhook", json=data, headers=headers)
    assert response.status_code == 200


@pytest.mark.parametrize("header, enabled, expected", [
    ("webhook-id", True, 1), ("Idempotency-Key", True, 1), ("webhook-id", False, 2),
])
def test_redelivery_is_opt_in(reviewer_webhook, monkeypatch, header, enabled, expected):
    client, perform = reviewer_webhook
    monkeypatch.setitem(get_settings().gitlab, "webhook_delivery_deduplication", enabled)
    for _ in range(2):
        _post(client, _reviewer_event(), "same-delivery", header=header)
    assert perform.await_count == expected


def test_different_projects_and_instances_are_independent(reviewer_webhook):
    client, perform = reviewer_webhook
    _post(client, _reviewer_event(7), "shared-id")
    _post(client, _reviewer_event(8), "shared-id")
    _post(client, _reviewer_event(7), "shared-id", instance="https://another.gitlab.example.com")
    assert perform.await_count == 3


def test_reviewer_identity_lookup_failure_can_retry(reviewer_webhook, monkeypatch):
    client, perform = reviewer_webhook
    monkeypatch.setattr(gitlab_webhook, "is_bot_assigned_as_reviewer", REAL_REVIEWER_CHECK)
    lookup = mock.AsyncMock(side_effect=[None, 516])
    monkeypatch.setattr(gitlab_webhook, "_get_bot_user_id", lookup)

    _post(client, _reviewer_event(), "lookup-retry")
    _post(client, _reviewer_event(), "lookup-retry")
    assert lookup.await_count == 2
    assert perform.await_count == 1


def test_external_host_setting_enables_retry_guard(reviewer_webhook, monkeypatch, tmp_path):
    client, perform = reviewer_webhook
    external = tmp_path / "host.toml"
    external.write_text("[gitlab]\nwebhook_delivery_deduplication = true\n", encoding="utf-8")
    monkeypatch.setitem(get_settings().config, "extra_config_url", str(external))
    monkeypatch.setitem(get_settings().gitlab, "webhook_delivery_deduplication", False)

    _post(client, _reviewer_event(), "external-opt-in")
    _post(client, _reviewer_event(), "external-opt-in")
    assert perform.await_count == 1


@pytest.mark.asyncio
async def test_missing_delivery_id_bypasses_dedup_host_probe(monkeypatch):
    settings = copy.deepcopy(global_settings)
    settings.set("CONFIG.EXTRA_CONFIG_URL", "https://example.invalid/host.toml")
    host_load = mock.Mock(side_effect=AssertionError("No dedup host probe without a delivery ID"))
    dispatch = mock.AsyncMock(return_value=True)
    monkeypatch.setattr(gitlab_webhook, "apply_host_settings", host_load)
    monkeypatch.setattr(gitlab_webhook, "process_gitlab_event", dispatch)
    with request_cycle_context({"settings": settings}):
        await gitlab_webhook.process_verified_gitlab_delivery(
            _reviewer_event(), {}, delivery_id=None, instance="https://gitlab.example.com")
    host_load.assert_not_called()
    dispatch.assert_awaited_once()


def test_plain_note_does_not_fetch_external_delivery_settings(reviewer_webhook, monkeypatch):
    client, perform = reviewer_webhook
    monkeypatch.setitem(get_settings().config, "extra_config_url", "https://example.invalid/host.toml")
    host_load = mock.Mock(side_effect=RuntimeError("plain notes need no host settings"))
    monkeypatch.setattr(gitlab_webhook, "apply_host_settings", host_load)
    note = {
        "object_kind": "note", "event_type": "note", "project_id": 7,
        "user": {"username": "commenter", "id": 3},
        "object_attributes": {"id": 42, "note": "Looks good to me"},
        "merge_request": {"url": MR_URL},
    }

    _post(client, note, "ordinary-note")
    host_load.assert_not_called()
    perform.assert_not_awaited()


def test_external_host_load_failure_preserves_revocation_and_host_exclusion(reviewer_webhook, monkeypatch):
    client, perform = reviewer_webhook
    monkeypatch.setitem(get_settings().config, "extra_config_url", "https://[broken")
    monkeypatch.setattr(gitlab_webhook, "apply_host_settings", mock.Mock(side_effect=ValueError("bad URL")))

    _post(client, _reviewer_event(), "host-load-error")
    perform.assert_not_awaited()  # The upstream Host-first policy fails closed.
    gitlab_webhook.revoke_auto_approval_on_push.assert_called_once()


def test_extra_host_exclusion_precedes_repository_override(reviewer_webhook, monkeypatch, tmp_path):
    client, perform = reviewer_webhook
    external = tmp_path / "host.toml"
    external.write_text("\n".join([
        "[gitlab]",
        "webhook_delivery_deduplication = true",
        "[config]",
        'ignore_pr_title = ["^WIP"]',
        "",
    ]), encoding="utf-8")
    monkeypatch.setitem(get_settings().config, "extra_config_url", str(external))
    monkeypatch.setitem(get_settings().config, "ignore_pr_title", [])
    monkeypatch.setitem(get_settings().gitlab, "webhook_delivery_deduplication", False)
    monkeypatch.setitem(get_settings().config, "include_pr_labels", [])
    monkeypatch.setitem(get_settings().config, "ignore_pr_authors", [])
    monkeypatch.setattr(gitlab_webhook, "should_process_pr_logic", REAL_SHOULD_PROCESS_PR_LOGIC)
    repo_settings = mock.AsyncMock()
    monkeypatch.setattr(gitlab_webhook, "apply_new_mr_repo_settings", repo_settings)
    payload = _reviewer_event()
    payload["object_attributes"].update(action="open", title="WIP: excluded by Host")
    _post(client, payload, "host-policy-precedence")
    perform.assert_not_awaited()
    repo_settings.assert_not_awaited()



@pytest.mark.asyncio
async def test_cached_remote_host_config_enables_dedup_without_tempfile(monkeypatch):
    # The merged Host-first loader can reuse frozen bytes without writing /tmp.
    source = "https://gitlab.example.com/host.toml"
    settings = copy.deepcopy(global_settings)
    settings.set("CONFIG.EXTRA_CONFIG_URL", source)
    settings.set("GITLAB.WEBHOOK_DELIVERY_DEDUPLICATION", False)
    dispatched = mock.AsyncMock(return_value=True)
    monkeypatch.setattr(gitlab_webhook, "process_gitlab_event", dispatched)

    with (
        request_cycle_context({
            "settings": settings,
            "extra_config_payloads": {
                source: b"[gitlab]\nwebhook_delivery_deduplication = true\n",
            },
        }),
        mock.patch("pr_agent.git_providers.utils.tempfile.mkstemp",
                   side_effect=OSError("read-only temporary directory")) as create_temp,
    ):
        for _ in range(2):
            await gitlab_webhook.process_verified_gitlab_delivery(
                _reviewer_event(), {}, delivery_id="cached-host", instance="https://gitlab.example.com")

    create_temp.assert_not_called()
    assert dispatched.await_count == 1


def test_missing_retry_id_does_not_deduplicate_event_or_webhook_uuid(reviewer_webhook):
    client, perform = reviewer_webhook
    headers = {
        "X-Gitlab-Token": "test-secret",
        "X-Gitlab-Event-UUID": "recursive-event-id",
        "X-Gitlab-Webhook-UUID": "not-a-retry-id",
    }
    for _ in range(2):
        assert client.post("/webhook", json=_reviewer_event(), headers=headers).status_code == 200

    assert perform.await_count == 2


def test_missing_project_identity_fails_open(reviewer_webhook):
    client, perform = reviewer_webhook
    payload = _reviewer_event()
    payload.pop("project")
    _post(client, payload, "no-project-id")
    _post(client, payload, "no-project-id")
    assert perform.await_count == 2


def test_unauthorized_request_cannot_poison_delivery_cache(reviewer_webhook):
    client, perform = reviewer_webhook
    payload = _reviewer_event()
    denied = client.post("/webhook", json=payload, headers={
        "X-Gitlab-Token": "wrong-secret", "webhook-id": "auth-first",
        "X-Gitlab-Instance": "https://gitlab.example.com",
    })
    assert denied.status_code == 401
    assert not gitlab_webhook._completed_webhook_deliveries
    _post(client, payload, "auth-first")
    assert perform.await_count == 1


@pytest.mark.parametrize("headers", [
    {"webhook-id": "one", "Idempotency-Key": "two"},
    {"webhook-id": " "},
    {"webhook-id": "x" * 129},
])
def test_invalid_retry_ids_fail_open(reviewer_webhook, headers):
    client, perform = reviewer_webhook
    headers = {"X-Gitlab-Token": "test-secret", **headers}
    for _ in range(2):
        assert client.post("/webhook", json=_reviewer_event(), headers=headers).status_code == 200
    assert perform.await_count == 2


def test_distinct_recursive_actions_with_same_event_uuid_are_not_suppressed(reviewer_webhook):
    client, perform = reviewer_webhook
    headers = {"X-Gitlab-Event-UUID": "parent-event"}
    for delivery in ("action-a", "action-b"):
        assert client.post("/webhook", json=_reviewer_event(), headers={
            "X-Gitlab-Token": "test-secret", "webhook-id": delivery, **headers,
        }).status_code == 200

    assert perform.await_count == 2


@pytest.mark.parametrize("event", ["open", "push", "draft-ready"])
def test_other_merge_request_actions_are_suppressed_only_on_retry(reviewer_webhook, monkeypatch, event):
    client, perform = reviewer_webhook
    monkeypatch.setattr(gitlab_webhook, "apply_new_mr_repo_settings", mock.AsyncMock())
    monkeypatch.setitem(get_settings().gitlab, "handle_push_trigger", True)
    monkeypatch.setitem(get_settings().gitlab, "push_commands", ["/review"])
    payload = _reviewer_event()
    if event == "open":
        payload["object_attributes"]["action"] = "open"
    elif event == "push":
        payload["object_attributes"]["oldrev"] = "previous-head"
    else:
        payload["changes"] = {"draft": {"previous": True, "current": False}}

    _post(client, payload, "same-" + event)
    _post(client, payload, "same-" + event)
    assert perform.await_count == 1
    assert perform.await_args.args[0] == ("push_commands" if event == "push" else "pr_commands")
    gitlab_webhook.is_bot_assigned_as_reviewer.assert_not_awaited()



@pytest.mark.parametrize("failure", ["rejected", "exception"])
def test_failed_approval_revocation_keeps_delivery_retryable(reviewer_webhook, monkeypatch, failure):
    client, perform = reviewer_webhook
    provider = mock.Mock()
    provider.revoke_auto_approval.side_effect = (
        [RuntimeError("GitLab API unavailable"), True] if failure == "exception" else [False, True]
    )
    monkeypatch.setattr(gitlab_webhook, "revoke_auto_approval_on_push", REAL_REVOKE_APPROVAL)
    monkeypatch.setattr(gitlab_webhook, "get_git_provider_with_context", lambda url: provider)
    monkeypatch.setattr(gitlab_webhook, "apply_host_settings", mock.Mock())
    monkeypatch.setitem(get_settings().config, "enable_auto_approval", True)
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setitem(get_settings().gitlab, "handle_push_trigger", True)
    monkeypatch.setitem(get_settings().gitlab, "push_commands", ["/review"])
    payload = _reviewer_event()
    payload["object_attributes"]["oldrev"] = "previous-head"

    _post(client, payload, "revocation-retry")
    _post(client, payload, "revocation-retry")
    _post(client, payload, "revocation-retry")
    assert provider.revoke_auto_approval.call_count == 2
    assert perform.await_count == 2


def test_busy_push_slot_does_not_complete_a_delivery(reviewer_webhook, monkeypatch):
    client, perform = reviewer_webhook
    monkeypatch.setitem(get_settings().gitlab, "handle_push_trigger", True)
    monkeypatch.setitem(get_settings().gitlab, "push_commands", ["/review"])
    slot_available = False

    @asynccontextmanager
    async def push_slot(*_args, **kwargs):
        # The outer delivery slot is free; only the inner per-MR push slot is busy.
        yield slot_available if kwargs.get("allow_backlog") else True

    monkeypatch.setattr(gitlab_webhook, "push_trigger_slot", push_slot)
    payload = _reviewer_event()
    payload["object_attributes"]["oldrev"] = "previous-head"

    _post(client, payload, "busy-delivery")
    assert perform.await_count == 0
    slot_available = True
    _post(client, payload, "busy-delivery")
    _post(client, payload, "busy-delivery")
    assert perform.await_count == 1


def test_skipped_auto_command_remains_retryable(reviewer_webhook):
    client, perform = reviewer_webhook
    perform.return_value = RequestOutcome.SKIPPED
    _post(client, _reviewer_event(), "policy-skipped")
    _post(client, _reviewer_event(), "policy-skipped")
    assert perform.await_count == 2



@pytest.mark.asyncio
async def test_concurrent_retry_cannot_run_in_parallel(monkeypatch):
    monkeypatch.setitem(get_settings().gitlab, "webhook_delivery_deduplication", True)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def slow_process(*_args):
        calls.append(True)
        started.set()
        await release.wait()
        return True

    monkeypatch.setattr(gitlab_webhook, "process_gitlab_event", slow_process)
    async def deliver():
        return await gitlab_webhook.process_verified_gitlab_delivery(
            _reviewer_event(), {}, delivery_id="active-id", instance="https://gitlab.example.com")

    first = asyncio.create_task(deliver())
    try:
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.wait_for(deliver(), 3)
        assert len(calls) == 1
    finally:
        release.set()
        await first

    await deliver()
    assert len(calls) == 1, "a completed delivery should also be skipped"


@pytest.mark.asyncio
async def test_failed_and_exceptional_attempts_can_retry(monkeypatch):
    monkeypatch.setitem(get_settings().gitlab, "webhook_delivery_deduplication", True)
    process = mock.AsyncMock(side_effect=[False, RuntimeError("transient"), True])
    monkeypatch.setattr(gitlab_webhook, "process_gitlab_event", process)

    async def deliver():
        return await gitlab_webhook.process_verified_gitlab_delivery(
            _reviewer_event(), {}, delivery_id="retry-after-failure", instance=None)

    await deliver()
    with pytest.raises(RuntimeError, match="transient"):
        await deliver()
    await deliver()
    await deliver()
    assert process.await_count == 3


@pytest.mark.asyncio
async def test_noop_delivery_is_not_marked_as_a_completed_command(monkeypatch):
    monkeypatch.setitem(get_settings().gitlab, "webhook_delivery_deduplication", True)
    process = mock.AsyncMock(side_effect=[None, True])
    monkeypatch.setattr(gitlab_webhook, "process_gitlab_event", process)

    for _ in range(2):
        await gitlab_webhook.process_verified_gitlab_delivery(
            _reviewer_event(), {}, delivery_id="noop-attempt", instance="https://gitlab.example.com")
    assert process.await_count == 2



@pytest.mark.asyncio
async def test_failed_delivery_cache_entries_expire(monkeypatch):
    monkeypatch.setitem(get_settings().gitlab, "webhook_delivery_deduplication", True)
    cache = DefaultDictWithTimeout(float, ttl=0, refresh_interval=0, update_key_time_on_get=False)
    monkeypatch.setattr(gitlab_webhook, "_completed_webhook_deliveries", cache)
    monkeypatch.setattr(gitlab_webhook, "process_gitlab_event", mock.AsyncMock(return_value=False))

    for delivery_id in ("failed-one", "failed-two"):
        await gitlab_webhook.process_verified_gitlab_delivery(
            _reviewer_event(), {}, delivery_id=delivery_id, instance="https://gitlab.example.com")
        if delivery_id == "failed-one":
            await asyncio.sleep(0.01)  # Ensure the next delivery observes expiration even on coarse clocks.
    assert len(cache) == 1, "failed deliveries must not accumulate past the TTL"


@pytest.mark.asyncio
async def test_cancelled_attempt_does_not_block_retry(monkeypatch):
    monkeypatch.setitem(get_settings().gitlab, "webhook_delivery_deduplication", True)
    started = asyncio.Event()

    async def cancelled_process(*_args):
        started.set()
        await asyncio.Future()

    process = mock.AsyncMock(side_effect=cancelled_process)
    monkeypatch.setattr(gitlab_webhook, "process_gitlab_event", process)

    async def deliver():
        return await gitlab_webhook.process_verified_gitlab_delivery(
            _reviewer_event(), {}, delivery_id="cancelled-id", instance=None)

    task = asyncio.create_task(deliver())
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    process.side_effect = None
    process.return_value = True
    await deliver()
    assert process.await_count == 2


@pytest.mark.asyncio
async def test_lambda_handoff_preserves_retry_headers_in_warm_process(monkeypatch):
    monkeypatch.setitem(get_settings().gitlab, "webhook_delivery_deduplication", True)
    monkeypatch.setitem(get_settings().gitlab, "personal_access_token", "fixture-api-token")
    process = mock.AsyncMock(return_value=True)
    monkeypatch.setattr(gitlab_webhook, "process_gitlab_event", process)
    monkeypatch.setattr(gitlab_webhook, "restore_gitlab_handoff_auth", mock.AsyncMock())
    event = lambda_handoff.build_handoff_event("gitlab", _reviewer_event(), {
        "webhook-id": "lambda-message",
        "idempotency-key": "lambda-message",
        "x-gitlab-event-uuid": "shared-recursive-event",
        "x-gitlab-instance": "https://gitlab.example.com",
        "x-gitlab-token": "must-not-be-forwarded",
    })
    handoff = lambda_handoff.extract_handoff(event)
    assert "x-gitlab-token" not in handoff["headers"]
    assert handoff["headers"]["idempotency-key"] == "lambda-message"

    await lambda_handoff._run_gitlab_handoff(handoff)
    await lambda_handoff._run_gitlab_handoff(handoff)
    assert process.await_count == 1


@pytest.mark.asyncio
async def test_lambda_legacy_event_uuid_is_for_tracing_not_deduplication(monkeypatch):
    monkeypatch.setitem(get_settings().gitlab, "webhook_delivery_deduplication", True)
    process = mock.AsyncMock(return_value=True)
    logger = mock.Mock()
    monkeypatch.setattr(lambda_handoff, "get_logger", lambda: logger)
    monkeypatch.setattr(gitlab_webhook, "process_gitlab_event", process)
    monkeypatch.setattr(gitlab_webhook, "restore_gitlab_handoff_auth", mock.AsyncMock())
    event = lambda_handoff.build_handoff_event("gitlab", _reviewer_event(), {
        "x-gitlab-event-uuid": "old-gitlab-event", "x-gitlab-token": "never-forward",
    })
    handoff = lambda_handoff.extract_handoff(event)
    await lambda_handoff._run_gitlab_handoff(handoff)
    await lambda_handoff._run_gitlab_handoff(handoff)

    assert process.await_count == 2
    logged_deliveries = [
        call.kwargs["artifact"].get("delivery")
        for call in logger.info.call_args_list
        if isinstance(call.kwargs.get("artifact"), dict)
    ]
    assert "old-gitlab-event" in logged_deliveries


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [
    "complete", "partial", "unknown", "failed", "skipped", "approval", "status", "sink",
])
async def test_batch_completion_requires_every_command_to_opt_in(monkeypatch, outcome):
    settings = copy.deepcopy(global_settings)
    settings.set("GITLAB.WEBHOOK_DELIVERY_DEDUPLICATION", True)
    settings.set("CONFIG.EXTRA_CONFIG_URL", "")
    settings.set("CONFIG.DISABLE_AUTO_FEEDBACK", False)
    settings.set("CONFIG.ENABLE_AUTO_APPROVAL", outcome == "approval")
    settings.set("PR_REVIEWER.PUBLISH_MERGE_RECOMMENDATION_STATUS", outcome == "status")
    settings.set("PUSH_OUTPUTS.ENABLE", outcome == "sink")
    monkeypatch.setattr(gitlab_webhook, "should_process_pr_logic", lambda _: True)
    monkeypatch.setattr(gitlab_webhook, "get_pr_commands", lambda _: ["/review", "/describe"])
    calls = []

    class Agent:
        async def handle_request(self, _url, command):
            calls.append(command[0])
            if outcome == "unknown":
                return True
            if outcome == "skipped" and command[0] == "/describe":
                return RequestOutcome.SKIPPED
            if command[0] == "/describe" and outcome == "failed":
                record_command_failure()
            if command[0] == "/describe" and outcome == "partial" and calls.count("/describe") == 1:
                return True
            get_run_details().webhook_delivery_completed = True
            return True

    async def process(data, log):
        return await gitlab_webhook._perform_commands_gitlab("pr_commands", Agent(), MR_URL, log, data)

    monkeypatch.setattr(gitlab_webhook, "process_gitlab_event", process)
    with request_cycle_context({"settings": settings}):
        for _ in range(3):
            await gitlab_webhook.process_verified_gitlab_delivery(
                _reviewer_event(), {}, delivery_id="batch", instance=None)
    attempts = 1 if outcome == "complete" else 2 if outcome == "partial" else 3
    assert calls == ["/review", "/describe"] * attempts


@pytest.mark.parametrize("completed, reaction_fails, expected_calls", [
    (True, False, 1), (False, False, 2), (True, True, 1),
])
def test_note_completion_requires_explicit_success(
    reviewer_webhook, monkeypatch, completed, reaction_fails, expected_calls
):
    client, _ = reviewer_webhook
    provider = mock.Mock()
    if reaction_fails:
        provider.react_to_outcome.side_effect = RuntimeError("reaction unavailable")
    monkeypatch.setattr(gitlab_webhook, "apply_host_settings", mock.Mock())
    monkeypatch.setattr(gitlab_webhook, "get_git_provider_with_context", lambda pr_url: provider)
    calls = []

    async def run_command(_url, body, _log_context, _sender_id, notify=None):
        calls.append(body)
        notify()
        get_run_details().webhook_delivery_completed = completed
        return True

    monkeypatch.setattr(gitlab_webhook, "handle_request", run_command)
    payload = {
        "object_kind": "note", "event_type": "note", "project_id": 7,
        "user": {"username": "commenter", "id": 3},
        "object_attributes": {"id": 42, "note": "/review"},
        "merge_request": {"url": MR_URL},
    }
    for _ in range(2):
        _post(client, payload, "note-message")
    assert calls == ["/review"] * expected_calls
    assert provider.react_to_outcome.call_args_list == [mock.call(42, True)] * expected_calls


def test_delivery_deduplication_is_host_only():
    assert is_repo_host_only_key("gitlab", "webhook_delivery_deduplication")


@pytest.mark.asyncio
async def test_authenticated_tenant_is_part_of_delivery_identity(monkeypatch):
    settings = copy.deepcopy(global_settings)
    settings.set("GITLAB.WEBHOOK_DELIVERY_DEDUPLICATION", True)
    settings.set("CONFIG.EXTRA_CONFIG_URL", "")
    process = mock.AsyncMock(return_value=True)
    monkeypatch.setattr(gitlab_webhook, "process_gitlab_event", process)
    for tenant in ("tenant-a", "tenant-b", "tenant-a"):
        with request_cycle_context({"settings": settings, "gitlab_webhook_secret_name": tenant}):
            await gitlab_webhook.process_verified_gitlab_delivery(
                _reviewer_event(), {}, delivery_id="shared-id", instance=None)
    assert process.await_count == 2
