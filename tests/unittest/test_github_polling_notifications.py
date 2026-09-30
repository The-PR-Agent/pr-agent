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
from multidict import MultiDict

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
async def _server(fallback, latest=None, api_prefix=""):
    async def latest_handler(request):
        return web.json_response(latest if latest is not None else _comment(99, "Other discussion"))

    app = web.Application()
    app.router.add_get("/latest", latest_handler)
    app.router.add_get(f"{api_prefix}/repos/owner/repo/issues/1/comments", fallback)
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
async def test_fallback_reads_the_declared_last_comment_page():
    selected = _comment(102)
    requested_pages = []

    async def fallback(request):
        page = request.query.get("page")
        requested_pages.append(page)
        if page is None:
            base_url = f"{request.scheme}://{request.host}{request.path}"
            return web.json_response(
                [_comment(1, "No mention")],
                headers={"Link": f'<{base_url}?per_page=100&page=2>; rel="next", '
                         f'<{base_url}?per_page=100&page=2>; rel="last"'},
            )
        assert page == "2"
        return web.json_response([selected, _comment(103, "No mention"), _comment(104, "No mention"),
                                  _comment(105, "No mention")])

    async with _server(fallback) as url, aiohttp.ClientSession() as session:
        handled = set()
        result = await github_polling.is_valid_notification(_notification(url), {}, handled, session, "bot")

    assert result == (True, handled, selected, "@bot /review", f"{url}/repos/owner/repo/pulls/1", "@bot")
    assert requested_pages == [None, "2"]


@pytest.mark.asyncio
async def test_fallback_combines_a_short_last_page_with_its_predecessor():
    selected = _comment(100)
    requested_pages = []

    async def fallback(request):
        page = request.query.get("page")
        requested_pages.append(page)
        if page is None:
            base_url = f"{request.scheme}://{request.host}{request.path}"
            return web.json_response(
                [_comment(1, "No mention")],
                headers={"Link": f'<{base_url}?per_page=100&page=2>; rel="next", '
                         f'<{base_url}?per_page=100&page=2>; rel="last"'},
            )
        if page == "2":
            return web.json_response([_comment(101, "No mention"), _comment(102, "No mention")])
        assert page == "1"
        return web.json_response([_comment(98, "No mention"), _comment(99, "No mention"), selected],
                                 headers={"Link": f'<{request.path}?page=2>; rel="next"'})

    async with _server(fallback) as url, aiohttp.ClientSession() as session:
        handled = set()
        result = await github_polling.is_valid_notification(_notification(url), {}, handled, session, "bot")

    assert result == (True, handled, selected, "@bot /review", f"{url}/repos/owner/repo/pulls/1", "@bot")
    assert requested_pages == [None, "2", "1", "2"]


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
        links = MultiDict()

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
    assert 0 < kwargs["timeout"].total <= 10
    assert kwargs["allow_redirects"] is True
    assert kwargs["max_redirects"] == 30


def _page_response(request, ids):
    page = int(request.query.get("page", 1))
    assert request.query["per_page"] == "100"
    last = max(1, (len(ids) + 99) // 100)
    links = []
    if page < last:
        links = [f'<{request.path}?page={value}&per_page=100>; rel="{rel}"'
                 for rel, value in (("next", page + 1), ("last", last))]
    return web.json_response([_comment(i) for i in ids[(page - 1) * 100:page * 100]],
                             headers={"Link": ", ".join(links)} if links else {})


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 4, 99, 100, 101, 103, 104, 199, 200, 201, 203, 204])
async def test_comment_history_returns_newest_four_with_bounded_requests(count):
    pages = []
    ids = list(range(1, count + 1))

    async def fallback(request):
        pages.append(int(request.query.get("page", 1)))
        return _page_response(request, ids)

    async with _server(fallback) as url, aiohttp.ClientSession() as session:
        comments = await github_polling._fetch_comment_history(session, f"{url}/repos/owner/repo/issues/1/comments", {})

    assert [comment["id"] for comment in comments] == ids[-4:]
    last = max(1, (count + 99) // 100)
    expected = [1] if last == 1 else [1, last]
    if last > 1 and 0 < count % 100 < 4:
        expected += [last - 1, last]
    assert pages == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["grow", "contract", "delete_boundary", "delete_terminal"])
async def test_comment_history_retries_changed_pages(change):
    ids = list(range(1, 202 if change in ("grow", "contract") else 103))
    calls = 0

    async def fallback(request):
        nonlocal calls
        calls += 1
        if change == "contract" and calls == 2:
            del ids[99:]
        elif calls == 3:
            if change == "grow":
                ids.extend([202, 203, 204])
            elif change == "delete_boundary":
                ids.remove(100)
            elif change == "delete_terminal":
                ids.remove(102)
        return _page_response(request, ids)

    async with _server(fallback) as url, aiohttp.ClientSession() as session:
        comments = await github_polling._fetch_comment_history(session, f"{url}/repos/owner/repo/issues/1/comments", {})

    assert [comment["id"] for comment in comments] == ids[-4:]
    assert calls <= 8


@pytest.mark.asyncio
async def test_notification_keeps_repeatedly_changing_tail_retryable():
    ids = list(range(1, 202))
    calls = 0

    async def fallback(request):
        nonlocal calls
        calls += 1
        if calls in (3, 7):
            ids.append(ids[-1] + 1)
        return _page_response(request, ids)

    async with _server(fallback) as url, aiohttp.ClientSession() as session:
        handled = set()
        result = await github_polling.is_valid_notification(_notification(url), {}, handled, session, "bot")

    assert result == (False, handled, github_polling._RETRY_POLLING_NOTIFICATION)
    assert handled == set()
    assert calls == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("page", ["", "0", "-1", "bad", "1.5", "%D9%A2", "2&page=3"])
async def test_comment_history_rejects_invalid_last_page(page):
    async def fallback(request):
        return web.json_response([_comment()], headers={"Link":
            f'<{request.path}?page=2>; rel="next", <{request.path}?page={page}>; rel="last"'})

    async with _server(fallback) as url, aiohttp.ClientSession() as session:
        with pytest.raises(github_polling._InvalidPaginationMetadata):
            await github_polling._fetch_comment_history(session, f"{url}/repos/owner/repo/issues/1/comments", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", ["next", "last"])
async def test_comment_history_rejects_duplicate_pagination_relations(relation):
    async def fallback(request):
        return web.json_response([_comment()], headers={"Link":
            f'<{request.path}?page=2>; rel="next", <{request.path}?page=2>; rel="last", '
            f'<{request.path}?page=3>; rel="{relation}"'})

    async with _server(fallback) as url, aiohttp.ClientSession() as session:
        with pytest.raises(github_polling._InvalidPaginationMetadata):
            await github_polling._fetch_comment_history(session, f"{url}/repos/owner/repo/issues/1/comments", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", ["next", "last"])
async def test_comment_history_rejects_incomplete_pagination_links(relation):
    async def fallback(request):
        return web.json_response([_comment()], headers={"Link": f'<{request.path}?page=2>; rel="{relation}"'})

    async with _server(fallback) as url, aiohttp.ClientSession() as session:
        with pytest.raises(github_polling._InvalidPaginationMetadata):
            await github_polling._fetch_comment_history(session, f"{url}/repos/owner/repo/issues/1/comments", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "/api/v3"])
async def test_comment_history_uses_only_page_number_from_link(prefix):
    pages = []

    async def fallback(request):
        pages.append(int(request.query.get("page", 1)))
        if len(pages) == 1:
            return web.json_response([_comment()], headers={"Link":
                '<https://other.invalid/unrelated?page=2>; rel="next", '
                '<https://other.invalid/unrelated?page=2>; rel="last"'})
        return web.json_response([_comment(i) for i in range(101, 105)])

    async with _server(fallback, api_prefix=prefix) as url, aiohttp.ClientSession() as session:
        comments = await github_polling._fetch_comment_history(
            session, f"{url}{prefix}/repos/owner/repo/issues/1/comments", {})

    assert [comment["id"] for comment in comments] == [101, 102, 103, 104]
    assert pages == [1, 2]


@pytest.mark.asyncio
async def test_comment_history_shares_deadline_across_pages(monkeypatch):
    calls = []
    monkeypatch.setattr(github_polling, "_get_polling_request_timeout", lambda: 0.1)

    async def fallback(request):
        calls.append(int(request.query.get("page", 1)))
        await asyncio.sleep(0.06)
        return _page_response(request, list(range(1, 205)))

    async with _server(fallback) as url, aiohttp.ClientSession() as session:
        with pytest.raises(asyncio.TimeoutError):
            await github_polling._fetch_comment_history(session, f"{url}/repos/owner/repo/issues/1/comments", {})
    assert calls == [1, 3]


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
