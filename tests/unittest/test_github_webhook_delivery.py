"""Tests for GitHub webhook delivery identity and idempotency."""

import asyncio
from concurrent.futures import ProcessPoolExecutor

import pytest
from starlette.background import BackgroundTasks
from starlette_context import request_cycle_context

from pr_agent.config_loader import get_settings
from pr_agent.servers import github_app
from pr_agent.servers.webhook_delivery import WebhookDeliveryStore, webhook_delivery_slot


def _slot(database_path, delivery_id, installation_id="1"):
    return webhook_delivery_slot(
        delivery_id,
        installation_id,
        database_path=str(database_path),
        lease_ttl=10,
        retention_ttl=30,
    )


def _claim_in_worker(database_path):
    async def claim():
        store = WebhookDeliveryStore(database_path, lease_ttl=10, retention_ttl=30)
        return await store.claim("1", "delivery-1") is not None

    return asyncio.run(claim())


@pytest.mark.asyncio
async def test_delivery_slot_suppresses_completed_duplicate(tmp_path):
    database_path = tmp_path / "deliveries.sqlite3"

    async with _slot(database_path, "delivery-1") as proceed:
        assert proceed is True

    async with _slot(database_path, "delivery-1") as proceed:
        assert proceed is False


def test_delivery_store_claim_is_atomic_across_workers(tmp_path):
    database_path = str(tmp_path / "deliveries.sqlite3")

    with ProcessPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(_claim_in_worker, [database_path, database_path]))

    assert claims.count(True) == 1
    assert claims.count(False) == 1


@pytest.mark.asyncio
async def test_delivery_slot_suppresses_concurrent_duplicate(tmp_path):
    database_path = tmp_path / "deliveries.sqlite3"
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first_delivery():
        async with _slot(database_path, "delivery-1") as proceed:
            assert proceed is True
            entered.set()
            await release.wait()

    first = asyncio.create_task(first_delivery())
    await entered.wait()

    async with _slot(database_path, "delivery-1") as proceed:
        assert proceed is False

    release.set()
    await first


@pytest.mark.asyncio
async def test_delivery_slot_keeps_distinct_deliveries_independent(tmp_path):
    database_path = tmp_path / "deliveries.sqlite3"

    async with _slot(database_path, "delivery-1") as first:
        async with _slot(database_path, "delivery-2") as second:
            assert first is True
            assert second is True


@pytest.mark.asyncio
async def test_failed_delivery_slot_can_be_retried(tmp_path):
    database_path = tmp_path / "deliveries.sqlite3"

    with pytest.raises(RuntimeError, match="agent failed"):
        async with _slot(database_path, "delivery-1") as proceed:
            assert proceed is True
            raise RuntimeError("agent failed")

    async with _slot(database_path, "delivery-1") as proceed:
        assert proceed is True


@pytest.mark.asyncio
async def test_cancelled_delivery_slot_can_be_retried(tmp_path):
    database_path = tmp_path / "deliveries.sqlite3"

    async def cancelled_delivery():
        async with _slot(database_path, "delivery-1") as proceed:
            assert proceed is True
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await cancelled_delivery()

    async with _slot(database_path, "delivery-1") as proceed:
        assert proceed is True


@pytest.mark.asyncio
async def test_expired_claim_replaces_owner_without_old_owner_completing(tmp_path, monkeypatch):
    from pr_agent.servers import webhook_delivery

    now = [1000.0]
    monkeypatch.setattr(webhook_delivery.time, "time", lambda: now[0])
    store = WebhookDeliveryStore(str(tmp_path / "deliveries.sqlite3"), lease_ttl=10, retention_ttl=30)

    first_token = await store.claim("1", "delivery-1")
    now[0] += 11
    second_token = await store.claim("1", "delivery-1")

    assert first_token
    assert second_token
    assert second_token != first_token
    assert await store.complete("1", "delivery-1", first_token) is False
    assert await store.complete("1", "delivery-1", second_token) is True


@pytest.mark.asyncio
async def test_github_webhook_route_forwards_delivery_id(monkeypatch):
    body = {"installation": {"id": 1}, "action": "created"}
    observed = []

    class Request:
        headers = {
            "X-GitHub-Event": "issue_comment",
            "X-GitHub-Delivery": "delivery-1",
        }

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
        ((body,), {"event": "issue_comment", "delivery_id": "delivery-1"}),
    ]


@pytest.mark.asyncio
async def test_handle_request_dispatches_a_delivery_only_once(monkeypatch, tmp_path):
    settings = get_settings()
    setting_name = "GITHUB_APP.WEBHOOK_DELIVERY_DATABASE_PATH"
    original_path = settings.get(setting_name, None)
    dispatched = []

    async def fake_dispatch(body, event, action):
        dispatched.append((body, event, action))

    body = {
        "installation": {"id": 1},
        "action": "created",
        "comment": {
            "body": "/review",
            "pull_request_url": "https://api.github.com/repos/org/repo/pulls/1",
            "id": 42,
        },
    }
    settings.set(setting_name, str(tmp_path / "deliveries.sqlite3"))
    monkeypatch.setattr(github_app, "_dispatch_request", fake_dispatch)

    try:
        await github_app.handle_request(body, "issue_comment", delivery_id="delivery-1")
        await github_app.handle_request(body, "issue_comment", delivery_id="delivery-1")
    finally:
        if original_path is None:
            settings.unset(setting_name, force=True)
        else:
            settings.set(setting_name, original_path)

    assert dispatched == [(body, "issue_comment", "created")]


@pytest.mark.asyncio
async def test_handle_request_keeps_distinct_delivery_ids_independent(monkeypatch, tmp_path):
    settings = get_settings()
    setting_name = "GITHUB_APP.WEBHOOK_DELIVERY_DATABASE_PATH"
    original_path = settings.get(setting_name, None)
    dispatched = []

    async def fake_dispatch(body, event, action):
        dispatched.append((event, action))

    body = {"installation": {"id": 1}, "action": "created"}
    settings.set(setting_name, str(tmp_path / "deliveries.sqlite3"))
    monkeypatch.setattr(github_app, "_dispatch_request", fake_dispatch)

    try:
        await github_app.handle_request(body, "issue_comment", delivery_id="delivery-1")
        await github_app.handle_request(body, "issue_comment", delivery_id="delivery-2")
    finally:
        if original_path is None:
            settings.unset(setting_name, force=True)
        else:
            settings.set(setting_name, original_path)

    assert dispatched == [("issue_comment", "created"), ("issue_comment", "created")]
