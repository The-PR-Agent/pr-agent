"""Tests for opt-in GitHub webhook delivery deduplication."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.background import BackgroundTasks
from starlette_context import request_cycle_context

from pr_agent.config_loader import global_settings
from pr_agent.servers import github_app
from pr_agent.servers import utils as servers_utils


@pytest.fixture
def delivery_clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(
        servers_utils.DefaultDictWithTimeout,
        "_DefaultDictWithTimeout__time",
        staticmethod(lambda: now[0]),
    )
    monkeypatch.setattr(github_app, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.fixture
def delivery_context(monkeypatch, delivery_clock):
    settings = copy.deepcopy(global_settings)
    settings.set("GITHUB_APP.WEBHOOK_DELIVERY_DEDUPLICATION", True)
    settings.set("GITHUB_APP.PUSH_TRIGGER_PENDING_TASKS_TTL", 300)
    monkeypatch.setattr(github_app, "_WEBHOOK_DELIVERY_TTL", 300)
    monkeypatch.setattr(servers_utils, "_push_trigger_states_by_ttl", {})
    monkeypatch.setattr(servers_utils, "_active_push_trigger_states", {})
    monkeypatch.setattr(
        github_app,
        "_completed_webhook_deliveries",
        servers_utils.DefaultDictWithTimeout(
            float, ttl=300, update_key_time_on_get=False
        ),
    )
    with request_cycle_context({"settings": settings}):
        yield settings


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_id", ["delivery-1", None])
async def test_github_webhook_route_forwards_delivery_id(monkeypatch, delivery_id):
    body = {"installation": {"id": 1}, "action": "created"}
    observed = []

    class Request:
        headers = {"X-GitHub-Event": "issue_comment"}
        if delivery_id is not None:
            headers["X-GitHub-Delivery"] = delivery_id

    async def fake_get_body(_request):
        return body

    async def fake_handle_request(*args, **kwargs):
        observed.append((args, kwargs))

    monkeypatch.setattr(github_app, "get_body", fake_get_body)
    monkeypatch.setattr(github_app, "handle_request", fake_handle_request)
    background_tasks = BackgroundTasks()

    with request_cycle_context({}):
        await github_app.handle_github_webhooks(background_tasks, Request(), object())
        await background_tasks()

    assert observed == [
        ((body,), {"event": "issue_comment", "delivery_id": delivery_id}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("enabled", "delivery_id"), [(False, "delivery-1"), (True, None), (True, "")])
async def test_handle_request_bypasses_deduplication_when_disabled_or_missing_id(
    monkeypatch, delivery_context, enabled, delivery_id
):
    delivery_context.set("GITHUB_APP.WEBHOOK_DELIVERY_DEDUPLICATION", enabled)
    dispatch = AsyncMock(return_value={})
    monkeypatch.setattr(github_app, "_dispatch_request", dispatch)
    body = {"action": "created"}

    assert await github_app.handle_request(body, "issue_comment", delivery_id=delivery_id) == {}
    assert await github_app.handle_request(body, "issue_comment", delivery_id=delivery_id) == {}

    assert dispatch.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [True, {}, None])
async def test_handle_request_suppresses_completed_or_intentionally_ignored_delivery(
    monkeypatch, delivery_context, result
):
    dispatch = AsyncMock(return_value=result)
    monkeypatch.setattr(github_app, "_dispatch_request", dispatch)
    body = {"action": "created"}

    await github_app.handle_request(body, "issue_comment", delivery_id="delivery-1")
    await github_app.handle_request(body, "issue_comment", delivery_id="delivery-1")

    dispatch.assert_awaited_once_with(body, "issue_comment", "created")


@pytest.mark.asyncio
async def test_handle_request_keeps_distinct_delivery_ids_independent(monkeypatch, delivery_context):
    dispatch = AsyncMock(return_value={})
    monkeypatch.setattr(github_app, "_dispatch_request", dispatch)
    body = {"action": "created"}

    await github_app.handle_request(body, "issue_comment", delivery_id="delivery-1")
    await github_app.handle_request(body, "issue_comment", delivery_id="delivery-2")

    assert dispatch.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed", [0, 301])
async def test_handle_request_suppresses_active_delivery_even_after_ttl(
    monkeypatch, delivery_context, delivery_clock, elapsed
):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def dispatch(_body, _event, _action):
        entered.set()
        await release.wait()
        return {}

    dispatch_mock = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr(github_app, "_dispatch_request", dispatch_mock)
    body = {"action": "created"}
    first = asyncio.create_task(github_app.handle_request(body, "issue_comment", delivery_id="delivery-1"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        delivery_clock[0] += elapsed
        await asyncio.wait_for(
            github_app.handle_request(body, "issue_comment", delivery_id="delivery-1"), 1
        )
        dispatch_mock.assert_awaited_once()
    finally:
        release.set()
        await asyncio.wait_for(first, 1)

    assert not servers_utils._active_push_trigger_states


@pytest.mark.asyncio
async def test_active_delivery_does_not_block_a_different_delivery(monkeypatch, delivery_context):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def dispatch(body, _event, _action):
        if body["comment_id"] == 1:
            entered.set()
            await release.wait()
        return {}

    dispatch_mock = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr(github_app, "_dispatch_request", dispatch_mock)
    first = asyncio.create_task(
        github_app.handle_request({"action": "created", "comment_id": 1}, "issue_comment", delivery_id="delivery-1")
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(
            github_app.handle_request({"action": "created", "comment_id": 2}, "issue_comment", delivery_id="delivery-2"),
            1,
        )
        assert dispatch_mock.await_count == 2
    finally:
        release.set()
        await asyncio.wait_for(first, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("agent failed"), asyncio.CancelledError()])
async def test_failed_or_cancelled_dispatch_can_retry(monkeypatch, delivery_context, error):
    dispatch = AsyncMock(side_effect=[error, {}])
    monkeypatch.setattr(github_app, "_dispatch_request", dispatch)
    body = {"action": "created"}

    with pytest.raises(type(error)):
        await github_app.handle_request(body, "issue_comment", delivery_id="delivery-1")
    assert not servers_utils._active_push_trigger_states

    await github_app.handle_request(body, "issue_comment", delivery_id="delivery-1")
    await github_app.handle_request(body, "issue_comment", delivery_id="delivery-1")

    assert dispatch.await_count == 2


@pytest.mark.asyncio
async def test_duplicate_does_not_extend_completed_delivery_ttl(monkeypatch, delivery_context, delivery_clock):
    dispatch = AsyncMock(return_value={})
    monkeypatch.setattr(github_app, "_dispatch_request", dispatch)
    body = {"action": "created"}

    await github_app.handle_request(body, "issue_comment", delivery_id="delivery-1")
    delivery_clock[0] = 299
    await github_app.handle_request(body, "issue_comment", delivery_id="delivery-1")
    assert dispatch.await_count == 1

    delivery_clock[0] = 301
    await github_app.handle_request(body, "issue_comment", delivery_id="delivery-1")
    assert dispatch.await_count == 2


@pytest.mark.asyncio
async def test_completed_delivery_ttl_starts_after_dispatch(monkeypatch, delivery_context, delivery_clock):
    async def dispatch(_body, _event, _action):
        delivery_clock[0] += 301
        return {}

    dispatch_mock = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr(github_app, "_dispatch_request", dispatch_mock)
    body = {"action": "created"}

    await github_app.handle_request(body, "issue_comment", delivery_id="delivery-1")
    delivery_clock[0] = 600
    await github_app.handle_request(body, "issue_comment", delivery_id="delivery-1")
    assert dispatch_mock.await_count == 1

    delivery_clock[0] = 602
    await github_app.handle_request(body, "issue_comment", delivery_id="delivery-1")
    assert dispatch_mock.await_count == 2
