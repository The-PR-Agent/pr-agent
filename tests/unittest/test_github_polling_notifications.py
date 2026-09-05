import asyncio
import json
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiohttp
import pytest
import requests
from aiohttp import web

from pr_agent.servers import github_polling


@pytest.fixture(autouse=True)
def isolate_notification_io(monkeypatch):
    monkeypatch.setattr(
        github_polling, "global_settings", SimpleNamespace(get=lambda key, default: default)
    )
    monkeypatch.setattr(github_polling, "get_logger", MagicMock())

    def reject_sync_http(*args, **kwargs):
        raise AssertionError("Notification fallback must not use synchronous HTTP")

    monkeypatch.setattr(requests, "get", reject_sync_http)


def _comment(comment_id=2, body="@bot /review", user="human"):
    return {"id": comment_id, "body": body, "user": {"login": user}}


def _notification(base_url):
    return {
        "reason": "mention",
        "subject": {
            "type": "PullRequest",
            "url": f"{base_url}/repos/owner/repo/pulls/1",
            "latest_comment_url": f"{base_url}/latest",
        },
    }


@asynccontextmanager
async def _server(fallback, latest=None):
    async def latest_handler(request):
        return web.json_response(latest if latest is not None else _comment(99, "Other discussion"))

    app = web.Application()
    app.router.add_get("/latest", latest_handler)
    app.router.add_get("/repos/owner/repo/issues/1/comments", fallback)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        host, port = runner.addresses[0]
        yield f"http://{host}:{port}"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_fallback_reuses_session_and_preserves_selection():
    selected = _comment(2)
    seen = []

    async def fallback(request):
        seen.append(request.headers["Authorization"])
        return web.Response(
            text=json.dumps([_comment(1, "@bot /ask old"), selected, _comment(3, ""), _comment(4, user="bot")]),
            content_type="text/plain",
        )

    connector = aiohttp.TCPConnector(limit=1)
    async with _server(fallback) as url, aiohttp.ClientSession(connector=connector) as session:
        # Check that the consumed latest-comment response releases the only
        # connection before fallback.
        handled = set()
        result = await github_polling.is_valid_notification(
            _notification(url), {"Authorization": "Bearer test-token"}, handled, session, "bot"
        )
    assert result == (True, handled, selected, "@bot /review", f"{url}/repos/owner/repo/pulls/1", "@bot")
    assert handled == {99}
    assert seen == ["Bearer test-token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("latest", [_comment(), _comment(2, "@bot /ask question")])
async def test_latest_mention_does_not_fetch_history(latest):
    calls = []

    async def fallback(request):
        calls.append(request.path)
        return web.json_response([])

    async with _server(fallback, latest) as url, aiohttp.ClientSession() as session:
        result = await github_polling.is_valid_notification(_notification(url), {}, set(), session, "bot")
    assert result[0] is True
    assert result[2] == latest
    assert not calls


@pytest.mark.asyncio
async def test_fallback_still_scans_only_four_comments():
    async def fallback(request):
        return web.json_response([_comment()] + [_comment(i, "No mention") for i in range(3, 7)])

    async with _server(fallback) as url, aiohttp.ClientSession() as session:
        handled = set()
        assert await github_polling.is_valid_notification(_notification(url), {}, handled, session, "bot") == (
            False, handled
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body"),
    [(401, "[]"), (403, "[]"), (429, "[]"), (500, "[]"), (200, "not json"), (200, "{}"), (200, "null")],
)
async def test_bad_fallback_response_is_rejected_and_connection_reusable(status, body):
    async def fallback(request):
        return web.Response(status=status, text=body)

    connector = aiohttp.TCPConnector(limit=1)
    async with _server(fallback) as url, aiohttp.ClientSession(connector=connector) as session:
        handled = set()
        assert await github_polling.is_valid_notification(_notification(url), {}, handled, session, "bot") == (
            False, handled
        )
        async with session.get(f"{url}/latest", timeout=aiohttp.ClientTimeout(total=2)) as response:
            assert response.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_stalled_fallback_yields_and_releases_connection(monkeypatch, cancel):
    entered = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(github_polling, "_get_polling_request_timeout", lambda: 2 if cancel else 0.05)

    async def fallback(request):
        entered.set()
        await release.wait()
        return web.json_response([])

    connector = aiohttp.TCPConnector(limit=1)
    async with _server(fallback) as url, aiohttp.ClientSession(connector=connector) as session:
        handled = set()
        task = asyncio.create_task(
            github_polling.is_valid_notification(_notification(url), {}, handled, session, "bot")
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            # Check that the event loop remains free while HTTP is stalled.
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    _ = await task
            else:
                assert await asyncio.wait_for(task, timeout=2) == (False, handled)
            async with session.get(f"{url}/latest", timeout=aiohttp.ClientTimeout(total=2)) as response:
                assert response.status == 200
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_fallback_has_explicit_timeout_and_redirect_limit(monkeypatch):
    calls = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def raise_for_status(self):
            pass

        async def json(self, **kwargs):
            return _comment(99, "Other discussion") if len(calls) == 1 else [_comment()]

    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    result = await github_polling.is_valid_notification(
        _notification("https://example.test"), {}, set(), Session(), "bot"
    )
    assert result[0] is True
    kwargs = calls[1][1]
    assert kwargs["timeout"].total == 10
    assert kwargs["allow_redirects"] is True
    assert kwargs["max_redirects"] == 30


@pytest.mark.parametrize(
    ("value", "expected"),
    [(10, 10), ("2.5", 2.5), (60, 60), (600, 60), (None, 10), (True, 10), (False, 10),
     (0, 10), (-1, 10), ("bad", 10), ([], 10), (float("nan"), 10), (float("inf"), 10)],
)
def test_polling_timeout_validation(monkeypatch, value, expected):
    monkeypatch.setattr(github_polling, "global_settings", SimpleNamespace(get=lambda key, default: value))
    assert github_polling._get_polling_request_timeout() == expected


def test_timeout_ignores_request_scoped_settings(monkeypatch):
    monkeypatch.setattr(github_polling, "global_settings", SimpleNamespace(get=lambda key, default: 12))
    monkeypatch.setattr(github_polling, "get_settings", lambda **kwargs: SimpleNamespace(get=lambda key, default: 60))
    assert github_polling._get_polling_request_timeout() == 12


def test_polling_timeout_default_matches_shipped_configuration():
    config_path = Path(github_polling.__file__).parents[1] / "settings" / "configuration.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["github"]["polling_request_timeout"] == github_polling.DEFAULT_POLLING_REQUEST_TIMEOUT
