import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette_context import request_cycle_context

from pr_agent.config_loader import global_settings
from pr_agent.servers import bitbucket_app
from pr_agent.servers import utils as servers_utils


@pytest.fixture
def push_env(monkeypatch):
    settings = copy.deepcopy(global_settings)
    settings.set("BITBUCKET_APP.HANDLE_PUSH_TRIGGER", True)
    settings.set("BITBUCKET_APP.PUSH_COMMANDS", ["/review"])
    monkeypatch.setattr(bitbucket_app, "get_settings", lambda: settings)
    monkeypatch.setattr(bitbucket_app, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(servers_utils, "_push_trigger_states_by_ttl", {})
    monkeypatch.setattr(servers_utils, "_active_push_trigger_states", {})
    return settings


def push_payload(second):
    return {
        "event": "pullrequest:updated",
        "data": {
            "actor": {"display_name": "alice"},
            "pullrequest": {
                "updated_on": f"2026-09-07T00:00:{second:02d}+00:00",
                "links": {"commits": {"href": "https://example.test/commits"}},
            },
        },
    }


async def test_bitbucket_backlog_reviews_latest_push_without_revalidating_old_event(push_env, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    queued = asyncio.Event()
    latest = {"second": 0}
    reviewed = []
    validated = []
    original_validate = bitbucket_app._validate_time_from_last_commit_to_pr_update

    async def validate(data):
        result = await original_validate(data)
        validated.append((data["data"]["pullrequest"]["updated_on"], result))
        if len(validated) == 2:
            queued.set()
        return result

    def fetch_commits(*args, **kwargs):
        second = latest["second"]
        return SimpleNamespace(status_code=200, json=lambda: {
            "values": [{
                "date": f"2026-09-07T00:00:{second:02d}+00:00",
                "author": {"user": {"display_name": "alice"}},
            }]
        })

    class Agent:
        async def handle_request(self, _url, _command):
            reviewed.append(latest["second"])
            if len(reviewed) == 1:
                entered.set()
                await release.wait()

    async def push(second):
        await bitbucket_app._perform_commands_bitbucket(
            "push_commands", Agent(), "https://example.test/pr/1", {}, push_payload(second)
        )

    monkeypatch.setattr(bitbucket_app.requests, "get", fetch_commits)
    monkeypatch.setattr(bitbucket_app, "_validate_time_from_last_commit_to_pr_update", validate)
    with request_cycle_context({"bitbucket_bearer_token": "test"}):
        first = asyncio.create_task(push(1))
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            latest["second"] = 10
            second = asyncio.create_task(push(11))
            await asyncio.wait_for(queued.wait(), 2)
            latest["second"] = 20
            await asyncio.wait_for(push(21), 2)
            assert reviewed == [0]
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
        finally:
            release.set()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
    assert reviewed == [0, 20]
    assert len(validated) == 3
    assert all(result for _, result in validated)
    assert not servers_utils._active_push_trigger_states


@pytest.mark.parametrize("enabled,valid", [(False, True), (True, False)])
async def test_bitbucket_disabled_or_invalid_push_does_not_reserve_slot(push_env, monkeypatch, enabled, valid):
    push_env.set("BITBUCKET_APP.HANDLE_PUSH_TRIGGER", enabled)
    validate = AsyncMock(return_value=valid)
    monkeypatch.setattr(bitbucket_app, "_validate_time_from_last_commit_to_pr_update", validate)
    agent = SimpleNamespace(handle_request=AsyncMock())

    def unexpected_slot(*args, **kwargs):
        pytest.fail("Rejected pushes must not consume the backlog slot")

    monkeypatch.setattr(bitbucket_app, "push_trigger_slot", unexpected_slot)
    await bitbucket_app._perform_commands_bitbucket(
        "push_commands", agent, "https://example.test/pr/1", {}, push_payload(1)
    )
    agent.handle_request.assert_not_awaited()
    assert validate.await_count == int(enabled)
