import asyncio
import json
import time
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


def _page_link(base_url, page, relationship, *, numeric_alias=False):
    if numeric_alias:
        path = "/repositories/123/issues/1/comments"
    else:
        path = "/repos/owner/repo/issues/1/comments"
    return f'<{base_url}{path}?per_page=4&page={page}>; rel="{relationship}"'


def _links(*values):
    return ", ".join(values)


class _FakeResponse:
    def __init__(self, body, *, link=None, delay=0, status=200):
        self.body = body
        self.headers = {} if link is None else {"Link": link}
        self.delay = delay
        self.status = status
        self.request_info = SimpleNamespace(real_url="https://example.test/comments")
        self.history = ()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    async def json(self, **kwargs):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.body


class _FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


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
        headers = {}

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
    assert kwargs["params"] == {"per_page": github_polling.POLLING_COMMENT_SCAN_LIMIT}


@pytest.mark.asyncio
async def test_comment_history_without_link_returns_only_ascending_tail():
    session = _FakeSession(_FakeResponse([_comment(comment_id) for comment_id in range(1, 7)]))
    url = "https://example.test/repos/owner/repo/issues/1/comments"

    comments = await github_polling._fetch_comment_history(session, url, {"Authorization": "test"})

    assert [comment["id"] for comment in comments] == [3, 4, 5, 6]
    assert session.calls == [
        (
            url,
            {
                "headers": {"Authorization": "test"},
                "params": {"per_page": 4},
                "timeout": session.calls[0][1]["timeout"],
                "allow_redirects": True,
                "max_redirects": 30,
            },
        )
    ]


@pytest.mark.asyncio
async def test_comment_history_fetches_declared_full_last_page_without_following_link_url():
    base_url = "https://example.test"
    url = f"{base_url}/repos/owner/repo/issues/1/comments"
    initial_link = ", ".join([
        _page_link(base_url, 2, "next"),
        _page_link(base_url, 3, "last"),
    ])
    last_link = _page_link(base_url, 2, "prev")
    session = _FakeSession(
        _FakeResponse([_comment(comment_id) for comment_id in range(1, 5)], link=initial_link),
        _FakeResponse([_comment(comment_id) for comment_id in range(9, 13)], link=last_link),
    )

    comments = await github_polling._fetch_comment_history(session, url, {})

    assert [comment["id"] for comment in comments] == [9, 10, 11, 12]
    assert [call[0] for call in session.calls] == [url, url]
    assert [call[1]["params"] for call in session.calls] == [{"per_page": 4}, {"per_page": 4, "page": 3}]
    assert [call[1]["allow_redirects"] for call in session.calls] == [True, False]


@pytest.mark.asyncio
async def test_comment_history_combines_previous_and_short_last_page_with_numeric_alias():
    base_url = "http://127.0.0.1:8123"
    url = f"{base_url}/repos/owner/repo/issues/1/comments"
    initial_link = ", ".join([
        _page_link(base_url, 2, "next", numeric_alias=True),
        _page_link(base_url, 3, "last", numeric_alias=True),
    ])
    last_link = _page_link(base_url, 2, "prev", numeric_alias=True)
    previous_link = ", ".join([
        _page_link(base_url, 3, "next", numeric_alias=True),
        _page_link(base_url, 3, "last", numeric_alias=True),
    ])
    session = _FakeSession(
        _FakeResponse([_comment(comment_id) for comment_id in range(1, 5)], link=initial_link),
        _FakeResponse([_comment(10), _comment(11)], link=last_link),
        _FakeResponse([_comment(comment_id) for comment_id in range(5, 10)], link=previous_link),
    )

    comments = await github_polling._fetch_comment_history(session, url, {})

    assert [comment["id"] for comment in comments] == [8, 9, 10, 11]
    assert [call[0] for call in session.calls] == [url, url, url]
    assert [call[1]["params"] for call in session.calls] == [
        {"per_page": 4},
        {"per_page": 4, "page": 3},
        {"per_page": 4, "page": 2},
    ]
    assert [call[1]["allow_redirects"] for call in session.calls] == [True, False, False]


@pytest.mark.asyncio
async def test_notification_selects_mention_from_declared_newest_page():
    base_url = "https://example.test"
    initial_link = ", ".join([_page_link(base_url, 2, "next"), _page_link(base_url, 3, "last")])
    selected = _comment(12)
    session = _FakeSession(
        _FakeResponse(_comment(99, "Other discussion")),
        _FakeResponse([_comment(comment_id, "No mention") for comment_id in range(1, 5)], link=initial_link),
        _FakeResponse([_comment(9, "No mention"), _comment(10, "No mention"),
                       _comment(11, "No mention"), selected]),
    )
    handled = set()

    result = await github_polling.is_valid_notification(
        _notification(base_url), {"Authorization": "test"}, handled, session, "bot"
    )

    assert result == (
        True, handled, selected, "@bot /review", f"{base_url}/repos/owner/repo/pulls/1", "@bot"
    )
    assert handled == {99}
    assert [call[1].get("params") for call in session.calls] == [None, {"per_page": 4},
                                                                  {"per_page": 4, "page": 3}]


@pytest.mark.asyncio
async def test_enterprise_notification_fetches_prefixed_comment_history():
    base_url = "https://example.test/api/v3"
    initial_link = _links(
        _page_link(base_url, 2, "next", numeric_alias=True),
        _page_link(base_url, 2, "last", numeric_alias=True),
    )
    selected = _comment(8)
    session = _FakeSession(
        _FakeResponse(_comment(99, "Other discussion")),
        _FakeResponse([_comment(comment_id, "No mention") for comment_id in range(1, 5)], link=initial_link),
        _FakeResponse([_comment(5, "No mention"), _comment(6, "No mention"),
                       _comment(7, "No mention"), selected]),
    )

    result = await github_polling.is_valid_notification(
        _notification(base_url), {"Authorization": "test"}, set(), session, "bot"
    )

    assert result[0] is True
    assert result[2] == selected
    assert session.calls[1][0] == f"{base_url}/repos/owner/repo/issues/1/comments"


@pytest.mark.asyncio
async def test_comment_history_refetches_last_page_when_pagination_advances():
    base_url = "https://example.test"
    url = f"{base_url}/repos/owner/repo/issues/1/comments"
    initial_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 3, "last"))
    advanced_link = _links(
        _page_link(base_url, 2, "prev"),
        _page_link(base_url, 4, "next"),
        _page_link(base_url, 4, "last"),
    )
    session = _FakeSession(
        _FakeResponse([_comment(comment_id) for comment_id in range(1, 5)], link=initial_link),
        _FakeResponse([_comment(comment_id) for comment_id in range(9, 13)], link=advanced_link),
        _FakeResponse([_comment(13)], link=_page_link(base_url, 3, "prev")),
    )

    comments = await github_polling._fetch_comment_history(session, url, {})

    assert [comment["id"] for comment in comments] == [10, 11, 12, 13]
    assert [call[1]["params"] for call in session.calls] == [
        {"per_page": 4},
        {"per_page": 4, "page": 3},
        {"per_page": 4, "page": 4},
    ]


@pytest.mark.asyncio
async def test_comment_history_fetches_new_previous_page_when_last_page_jumps():
    base_url = "https://example.test"
    url = f"{base_url}/repos/owner/repo/issues/1/comments"
    initial_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 3, "last"))
    advanced_link = _links(
        _page_link(base_url, 2, "prev"),
        _page_link(base_url, 4, "next"),
        _page_link(base_url, 5, "last"),
    )
    previous_link = _links(_page_link(base_url, 5, "next"), _page_link(base_url, 5, "last"))
    session = _FakeSession(
        _FakeResponse([_comment(comment_id) for comment_id in range(1, 5)], link=initial_link),
        _FakeResponse([_comment(comment_id) for comment_id in range(9, 13)], link=advanced_link),
        _FakeResponse([_comment(17)], link=_page_link(base_url, 4, "prev")),
        _FakeResponse([_comment(comment_id) for comment_id in range(13, 17)], link=previous_link),
    )

    comments = await github_polling._fetch_comment_history(session, url, {})

    assert [comment["id"] for comment in comments] == [14, 15, 16, 17]
    assert [call[1]["params"] for call in session.calls] == [
        {"per_page": 4},
        {"per_page": 4, "page": 3},
        {"per_page": 4, "page": 5},
        {"per_page": 4, "page": 4},
    ]


@pytest.mark.asyncio
async def test_comment_history_retries_when_refetched_last_page_advances_again():
    base_url = "https://example.test"
    url = f"{base_url}/repos/owner/repo/issues/1/comments"
    initial_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 3, "last"))
    refreshed_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 5, "last"))
    session = _FakeSession(
        _FakeResponse(
            [_comment(comment_id) for comment_id in range(1, 5)], link=initial_link, delay=0.01
        ),
        _FakeResponse(
            [_comment(comment_id) for comment_id in range(9, 13)],
            link=_links(_page_link(base_url, 4, "next"), _page_link(base_url, 4, "last")),
        ),
        _FakeResponse(
            [_comment(comment_id) for comment_id in range(13, 17)],
            link=_links(_page_link(base_url, 5, "next"), _page_link(base_url, 5, "last")),
        ),
        _FakeResponse([_comment(comment_id) for comment_id in range(1, 5)], link=refreshed_link),
        _FakeResponse([_comment(comment_id) for comment_id in range(17, 21)]),
    )

    comments = await github_polling._fetch_comment_history(session, url, {})

    assert [comment["id"] for comment in comments] == [17, 18, 19, 20]
    assert [call[1]["params"] for call in session.calls] == [
        {"per_page": 4},
        {"per_page": 4, "page": 3},
        {"per_page": 4, "page": 4},
        {"per_page": 4},
        {"per_page": 4, "page": 5},
    ]
    assert session.calls[3][1]["timeout"].total < session.calls[0][1]["timeout"].total


@pytest.mark.asyncio
async def test_notification_defers_when_terminal_page_advances_twice_per_scan():
    base_url = "https://example.test"
    initial_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 3, "last"))
    scan_responses = []
    for _ in range(2):
        scan_responses.extend([
            _FakeResponse([_comment(comment_id) for comment_id in range(1, 5)], link=initial_link),
            _FakeResponse(
                [_comment(comment_id) for comment_id in range(9, 13)],
                link=_links(_page_link(base_url, 4, "next"), _page_link(base_url, 4, "last")),
            ),
            _FakeResponse(
                [_comment(comment_id) for comment_id in range(13, 17)],
                link=_links(_page_link(base_url, 5, "next"), _page_link(base_url, 5, "last")),
            ),
        ])
    session = _FakeSession(_FakeResponse(_comment(99, "Other discussion")), *scan_responses)
    handled = set()

    result = await github_polling.is_valid_notification(
        _notification(base_url), {}, handled, session, "bot"
    )

    assert result == (False, handled, github_polling._RETRY_POLLING_NOTIFICATION)
    assert handled == set()
    assert [call[1].get("params") for call in session.calls] == [
        None,
        {"per_page": 4},
        {"per_page": 4, "page": 3},
        {"per_page": 4, "page": 4},
        {"per_page": 4},
        {"per_page": 4, "page": 3},
        {"per_page": 4, "page": 4},
    ]


@pytest.mark.asyncio
async def test_notification_recovers_when_deletion_removes_declared_last_page():
    base_url = "https://example.test"
    initial_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 3, "last"))
    refreshed_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 2, "last"))
    selected = _comment(8)
    session = _FakeSession(
        _FakeResponse(_comment(99, "Other discussion")),
        _FakeResponse(
            [_comment(comment_id, "No mention") for comment_id in range(1, 5)],
            link=initial_link,
            delay=0.01,
        ),
        _FakeResponse([], delay=0.01),
        _FakeResponse(
            [_comment(comment_id, "No mention") for comment_id in range(1, 5)],
            link=refreshed_link,
        ),
        _FakeResponse([
            _comment(5, "No mention"),
            _comment(6, "No mention"),
            _comment(7, "No mention"),
            selected,
        ]),
    )
    handled = set()

    result = await github_polling.is_valid_notification(
        _notification(base_url), {"Authorization": "test"}, handled, session, "bot"
    )

    assert result == (
        True, handled, selected, "@bot /review", f"{base_url}/repos/owner/repo/pulls/1", "@bot"
    )
    assert handled == {99}
    assert [call[1].get("params") for call in session.calls] == [
        None,
        {"per_page": 4},
        {"per_page": 4, "page": 3},
        {"per_page": 4},
        {"per_page": 4, "page": 2},
    ]
    assert session.calls[3][1]["timeout"].total < session.calls[1][1]["timeout"].total


@pytest.mark.asyncio
async def test_notification_retries_when_deletion_overlaps_adjacent_page_snapshots():
    base_url = "https://example.test"
    initial_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 3, "last"))
    last_link = _page_link(base_url, 2, "prev")
    previous_link = _links(_page_link(base_url, 3, "next"), _page_link(base_url, 3, "last"))
    selected = _comment(6)
    session = _FakeSession(
        _FakeResponse(_comment(99, "Other discussion")),
        _FakeResponse(
            [_comment(comment_id, "No mention") for comment_id in range(1, 5)],
            link=initial_link,
            delay=0.01,
        ),
        _FakeResponse([_comment(9, "No mention"), _comment(10, "No mention")], link=last_link),
        _FakeResponse([
            selected,
            _comment(7, "No mention"),
            _comment(8, "No mention"),
            _comment(9, "No mention"),
        ], link=previous_link, delay=0.01),
        _FakeResponse(
            [_comment(comment_id, "No mention") for comment_id in range(1, 5)],
            link=initial_link,
        ),
        _FakeResponse([_comment(10, "No mention")], link=last_link),
        _FakeResponse([
            _comment(5, "No mention"),
            selected,
            _comment(7, "No mention"),
            _comment(9, "No mention"),
        ], link=previous_link),
    )

    result = await github_polling.is_valid_notification(
        _notification(base_url), {"Authorization": "test"}, set(), session, "bot"
    )

    assert result[0] is True
    assert result[2] == selected
    comments_url = f"{base_url}/repos/owner/repo/issues/1/comments"
    assert [call[0] for call in session.calls] == [
        f"{base_url}/latest",
        comments_url,
        comments_url,
        comments_url,
        comments_url,
        comments_url,
        comments_url,
    ]
    assert [call[1].get("params") for call in session.calls] == [
        None,
        {"per_page": 4},
        {"per_page": 4, "page": 3},
        {"per_page": 4, "page": 2},
        {"per_page": 4},
        {"per_page": 4, "page": 3},
        {"per_page": 4, "page": 2},
    ]
    assert session.calls[4][1]["timeout"].total < session.calls[1][1]["timeout"].total


@pytest.mark.asyncio
async def test_notification_recovers_when_deletion_removes_stale_predecessor():
    base_url = "https://example.test"
    initial_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 3, "last"))
    selected = _comment(8)
    session = _FakeSession(
        _FakeResponse(_comment(99, "Other discussion")),
        _FakeResponse(
            [_comment(comment_id, "No mention") for comment_id in range(1, 5)],
            link=initial_link,
            delay=0.01,
        ),
        _FakeResponse(
            [_comment(9, "No mention"), _comment(10, "No mention")],
            link=_page_link(base_url, 2, "prev"),
        ),
        _FakeResponse([], delay=0.01),
        _FakeResponse([
            _comment(7, "No mention"),
            selected,
            _comment(9, "No mention"),
            _comment(10, "No mention"),
        ]),
    )
    handled = set()

    result = await github_polling.is_valid_notification(
        _notification(base_url), {"Authorization": "test"}, handled, session, "bot"
    )

    assert result == (
        True, handled, selected, "@bot /review", f"{base_url}/repos/owner/repo/pulls/1", "@bot"
    )
    assert handled == {99}
    assert [call[1].get("params") for call in session.calls] == [
        None,
        {"per_page": 4},
        {"per_page": 4, "page": 3},
        {"per_page": 4, "page": 2},
        {"per_page": 4},
    ]
    assert session.calls[4][1]["timeout"].total < session.calls[1][1]["timeout"].total


@pytest.mark.asyncio
async def test_comment_history_retries_when_predecessor_is_no_longer_adjacent():
    base_url = "https://example.test"
    url = f"{base_url}/repos/owner/repo/issues/1/comments"
    initial_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 3, "last"))
    refreshed_comments = [_comment(comment_id) for comment_id in range(7, 11)]
    session = _FakeSession(
        _FakeResponse([_comment(comment_id) for comment_id in range(1, 5)], link=initial_link),
        _FakeResponse([_comment(9), _comment(10)], link=_page_link(base_url, 2, "prev")),
        _FakeResponse([_comment(7), _comment(8)], link=_page_link(base_url, 1, "prev")),
        _FakeResponse(refreshed_comments),
    )

    assert await github_polling._fetch_comment_history(session, url, {}) == refreshed_comments
    assert [call[1]["params"] for call in session.calls] == [
        {"per_page": 4},
        {"per_page": 4, "page": 3},
        {"per_page": 4, "page": 2},
        {"per_page": 4},
    ]


@pytest.mark.asyncio
async def test_repeated_missing_predecessor_leaves_notification_eligible_for_retry():
    base_url = "https://example.test"
    initial_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 3, "last"))
    responses = [_FakeResponse(_comment(99, "Other discussion"))]
    for _ in range(2):
        responses.extend([
            _FakeResponse([_comment(comment_id) for comment_id in range(1, 5)], link=initial_link),
            _FakeResponse([_comment(9), _comment(10)], link=_page_link(base_url, 2, "prev")),
            _FakeResponse([]),
        ])
    session = _FakeSession(*responses)
    handled = set()

    result = await github_polling.is_valid_notification(
        _notification(base_url), {}, handled, session, "bot"
    )

    assert result == (False, handled, github_polling._RETRY_POLLING_NOTIFICATION)
    assert handled == set()
    assert [call[1].get("params") for call in session.calls] == [
        None,
        {"per_page": 4},
        {"per_page": 4, "page": 3},
        {"per_page": 4, "page": 2},
        {"per_page": 4},
        {"per_page": 4, "page": 3},
        {"per_page": 4, "page": 2},
    ]


@pytest.mark.asyncio
async def test_contradictory_predecessor_pagination_remains_invalid():
    base_url = "https://example.test"
    url = f"{base_url}/repos/owner/repo/issues/1/comments"
    initial_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 3, "last"))
    contradictory_link = _links(_page_link(base_url, 3, "next"), _page_link(base_url, 2, "last"))
    session = _FakeSession(
        _FakeResponse([_comment(comment_id) for comment_id in range(1, 5)], link=initial_link),
        _FakeResponse([_comment(9), _comment(10)], link=_page_link(base_url, 2, "prev")),
        _FakeResponse([_comment(7), _comment(8)], link=contradictory_link),
    )

    with pytest.raises(ValueError, match="Inconsistent pagination metadata"):
        await github_polling._fetch_comment_history(session, url, {})

    assert len(session.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "predecessor_link",
    [
        _page_link("https://example.test", 4, "last"),
        _links(
            _page_link("https://example.test", 4, "next"),
            _page_link("https://example.test", 4, "last"),
        ),
    ],
    ids=["mismatched-last", "mismatched-next-and-last"],
)
async def test_supplied_predecessor_relationship_mismatch_remains_invalid(predecessor_link):
    base_url = "https://example.test"
    url = f"{base_url}/repos/owner/repo/issues/1/comments"
    initial_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 3, "last"))
    session = _FakeSession(
        _FakeResponse([_comment(comment_id) for comment_id in range(1, 5)], link=initial_link),
        _FakeResponse([_comment(9), _comment(10)], link=_page_link(base_url, 2, "prev")),
        _FakeResponse([_comment(7), _comment(8)], link=predecessor_link),
    )

    with pytest.raises(ValueError, match="Inconsistent pagination metadata"):
        await github_polling._fetch_comment_history(session, url, {})

    assert len(session.calls) == 3


@pytest.mark.asyncio
async def test_comment_history_retries_overlapping_page_snapshots_only_once():
    base_url = "https://example.test"
    url = f"{base_url}/repos/owner/repo/issues/1/comments"
    initial_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 3, "last"))
    last_link = _page_link(base_url, 2, "prev")
    previous_link = _links(_page_link(base_url, 3, "next"), _page_link(base_url, 3, "last"))
    responses = []
    for _ in range(2):
        responses.extend([
            _FakeResponse([_comment(comment_id) for comment_id in range(1, 5)], link=initial_link),
            _FakeResponse([_comment(9), _comment(10)], link=last_link),
            _FakeResponse([_comment(comment_id) for comment_id in range(6, 10)], link=previous_link),
        ])
    session = _FakeSession(*responses)

    with pytest.raises(github_polling._CommentPaginationDrift, match="changed during the bounded scan"):
        await github_polling._fetch_comment_history(session, url, {})

    assert [call[0] for call in session.calls] == [url] * 6
    assert [call[1]["params"] for call in session.calls] == [
        {"per_page": 4},
        {"per_page": 4, "page": 3},
        {"per_page": 4, "page": 2},
        {"per_page": 4},
        {"per_page": 4, "page": 3},
        {"per_page": 4, "page": 2},
    ]


@pytest.mark.asyncio
async def test_repeated_page_overlap_leaves_notification_eligible_for_retry():
    base_url = "https://example.test"
    initial_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 3, "last"))
    last_link = _page_link(base_url, 2, "prev")
    previous_link = _links(_page_link(base_url, 3, "next"), _page_link(base_url, 3, "last"))
    responses = [_FakeResponse(_comment(99, "Other discussion"))]
    for _ in range(2):
        responses.extend([
            _FakeResponse([_comment(comment_id) for comment_id in range(1, 5)], link=initial_link),
            _FakeResponse([_comment(9), _comment(10)], link=last_link),
            _FakeResponse([_comment(comment_id) for comment_id in range(6, 10)], link=previous_link),
        ])
    session = _FakeSession(*responses)
    handled = set()

    result = await github_polling.is_valid_notification(
        _notification(base_url), {}, handled, session, "bot"
    )

    assert result == (False, handled, github_polling._RETRY_POLLING_NOTIFICATION)
    assert handled == set()


@pytest.mark.asyncio
async def test_comment_history_retries_disappeared_last_page_only_once():
    base_url = "https://example.test"
    url = f"{base_url}/repos/owner/repo/issues/1/comments"
    initial_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 3, "last"))
    session = _FakeSession(
        _FakeResponse([_comment(comment_id) for comment_id in range(1, 5)], link=initial_link),
        _FakeResponse([]),
        _FakeResponse([_comment(comment_id) for comment_id in range(1, 5)], link=initial_link),
        _FakeResponse([]),
    )

    with pytest.raises(github_polling._CommentPaginationDrift, match="changed during the bounded scan"):
        await github_polling._fetch_comment_history(session, url, {})

    assert [call[1]["params"] for call in session.calls] == [
        {"per_page": 4},
        {"per_page": 4, "page": 3},
        {"per_page": 4},
        {"per_page": 4, "page": 3},
    ]


@pytest.mark.asyncio
async def test_comment_history_recovers_when_deletion_contracts_to_one_page():
    base_url = "https://example.test"
    url = f"{base_url}/repos/owner/repo/issues/1/comments"
    initial_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 3, "last"))
    refreshed_comments = [_comment(7), _comment(8, "@bot /review")]
    session = _FakeSession(
        _FakeResponse([_comment(comment_id) for comment_id in range(1, 5)], link=initial_link),
        _FakeResponse([]),
        _FakeResponse(refreshed_comments),
    )

    assert await github_polling._fetch_comment_history(session, url, {}) == refreshed_comments
    assert [(call[0], call[1]["params"]) for call in session.calls] == [
        (url, {"per_page": 4}),
        (url, {"per_page": 4, "page": 3}),
        (url, {"per_page": 4}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "last_link",
    [None, '<https://example.test/repos/owner/repo/issues/1/comments?page=2>; rel="prev" garbage'],
)
async def test_nonempty_last_page_with_invalid_previous_link_is_not_treated_as_contraction(
        last_link,
):
    base_url = "https://example.test"
    url = f"{base_url}/repos/owner/repo/issues/1/comments"
    initial_link = _links(_page_link(base_url, 2, "next"), _page_link(base_url, 3, "last"))
    session = _FakeSession(
        _FakeResponse([_comment(comment_id) for comment_id in range(1, 5)], link=initial_link),
        _FakeResponse([_comment(9)], link=last_link),
    )

    with pytest.raises(ValueError):
        await github_polling._fetch_comment_history(session, url, {})

    assert len(session.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "link",
    [
        _links(
            '<https://evil.test/repos/owner/repo/issues/1/comments?page=2>; rel="next"',
            '<https://example.test/repos/owner/repo/issues/1/comments?page=3>; rel="last"',
        ),
        _links(
            '<https://user@example.test/repos/owner/repo/issues/1/comments?page=2>; rel="next"',
            '<https://example.test/repos/owner/repo/issues/1/comments?page=3>; rel="last"',
        ),
        _links(
            '<https://@example.test/repos/owner/repo/issues/1/comments?page=2>; rel="next"',
            '<https://example.test/repos/owner/repo/issues/1/comments?page=3>; rel="last"',
        ),
        _links(
            '<https://example.test/repos/owner/repo/issues/1/comments?page=2#secret>; rel="next"',
            '<https://example.test/repos/owner/repo/issues/1/comments?page=3>; rel="last"',
        ),
        _links(
            '<https://example.test/repos/owner/repo/issues/2/comments?page=2>; rel="next"',
            '<https://example.test/repos/owner/repo/issues/1/comments?page=3>; rel="last"',
        ),
        _links(
            '<https://example.test/repos/owner/other/issues/1/comments?page=2>; rel="next"',
            '<https://example.test/repos/owner/repo/issues/1/comments?page=3>; rel="last"',
        ),
        _links(
            '<https://example.test/repos/owner/repo/issues/1/comments?page=2&page=3>; rel="next"',
            '<https://example.test/repos/owner/repo/issues/1/comments?page=3>; rel="last"',
        ),
        _links(
            '<https://example.test/repos/owner/repo/issues/1/comments?page=two>; rel="next"',
            '<https://example.test/repos/owner/repo/issues/1/comments?page=3>; rel="last"',
        ),
        _links(
            '<https://example.test/repos/owner/repo/issues/1/comments?page=0>; rel="next"',
            '<https://example.test/repos/owner/repo/issues/1/comments?page=3>; rel="last"',
        ),
        _links(
            '<https://example.test/repos/owner/repo/issues/1/comments?page=-1>; rel="next"',
            '<https://example.test/repos/owner/repo/issues/1/comments?page=3>; rel="last"',
        ),
        '<https://example.test/repos/owner/repo/issues/1/comments?page=2>; rel="next" garbage',
        '<https://example.test/repos/owner/repo/issues/1/comments?page=2>; rel="next" "last"',
        '<https://example.test/repos/owner/repo/issues/1/comments?page=2>; rel="ne""xt"',
        '<https://example.test/repos/owner/repo/issues/1/comments?page=2>; rel="next"',
    ],
)
async def test_invalid_pagination_metadata_fails_before_any_followup_request(link):
    url = "https://example.test/repos/owner/repo/issues/1/comments"
    session = _FakeSession(_FakeResponse([_comment()], link=link))

    with pytest.raises(ValueError) as error:
        await github_polling._fetch_comment_history(session, url, {})

    assert len(session.calls) == 1
    assert "example.test" not in str(error.value)
    assert "evil.test" not in str(error.value)


@pytest.mark.asyncio
async def test_invalid_pagination_metadata_is_logged_without_secret_or_traceback(monkeypatch):
    marker = "SYNTHETIC_SECRET"
    link = (
        f'<https://alice:{marker}@example.test／.evil/repos/owner/repo/issues/1/comments?page=2>; rel="next", '
        '<https://example.test/repos/owner/repo/issues/1/comments?page=3>; rel="last"'
    )
    logger = MagicMock()
    monkeypatch.setattr(github_polling, "get_logger", lambda: logger)
    session = _FakeSession(
        _FakeResponse(_comment(99, "Other discussion")),
        _FakeResponse([_comment()], link=link),
    )

    result = await github_polling.is_valid_notification(
        _notification("https://example.test"), {}, set(), session, "bot"
    )

    assert result == (False, {99})
    logger.exception.assert_not_called()
    assert marker not in repr(logger.method_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("followup", [False, True])
async def test_comment_history_rejects_redirect_statuses(followup):
    base_url = "https://example.test"
    url = f"{base_url}/repos/owner/repo/issues/1/comments"
    initial_link = ", ".join([_page_link(base_url, 2, "next"), _page_link(base_url, 2, "last")])
    if followup:
        session = _FakeSession(
            _FakeResponse([_comment()], link=initial_link),
            _FakeResponse([_comment(2)], status=302),
        )
    else:
        session = _FakeSession(_FakeResponse([_comment()], status=302))

    with pytest.raises(aiohttp.ClientResponseError, match="302"):
        await github_polling._fetch_comment_history(session, url, {})

    assert len(session.calls) == (2 if followup else 1)


@pytest.mark.asyncio
async def test_short_last_page_requires_immediately_previous_page():
    base_url = "https://example.test"
    url = f"{base_url}/repos/owner/repo/issues/1/comments"
    initial_link = ", ".join([_page_link(base_url, 2, "next"), _page_link(base_url, 4, "last")])
    inconsistent_last_link = _page_link(base_url, 2, "prev")
    session = _FakeSession(
        _FakeResponse([_comment()], link=initial_link),
        _FakeResponse([_comment(13)], link=inconsistent_last_link),
    )

    with pytest.raises(ValueError, match="Inconsistent pagination metadata"):
        await github_polling._fetch_comment_history(session, url, {})

    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_comment_history_uses_one_decreasing_monotonic_deadline(monkeypatch):
    monkeypatch.setattr(github_polling, "_get_polling_request_timeout", lambda: 2)
    base_url = "https://example.test"
    url = f"{base_url}/repos/owner/repo/issues/1/comments"
    initial_link = ", ".join([_page_link(base_url, 2, "next"), _page_link(base_url, 2, "last")])
    session = _FakeSession(
        _FakeResponse([_comment()], link=initial_link, delay=0.01),
        _FakeResponse([_comment(comment_id) for comment_id in range(2, 6)], delay=0.01),
    )

    await github_polling._fetch_comment_history(session, url, {})

    assert session.calls[1][1]["timeout"].total < session.calls[0][1]["timeout"].total


@pytest.mark.asyncio
async def test_comment_history_detects_deadline_exhaustion_during_body_read(monkeypatch):
    monkeypatch.setattr(github_polling, "_get_polling_request_timeout", lambda: 0.01)
    url = "https://example.test/repos/owner/repo/issues/1/comments"
    session = _FakeSession(_FakeResponse([_comment()], delay=0.02))

    with pytest.raises(asyncio.TimeoutError):
        await github_polling._fetch_comment_history(session, url, {})

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_comment_history_detects_deadline_exhaustion_during_link_parsing(monkeypatch):
    monkeypatch.setattr(github_polling, "_get_polling_request_timeout", lambda: 0.01)
    original_parse = github_polling._parse_link_headers

    def slow_parse(values):
        time.sleep(0.02)
        return original_parse(values)

    monkeypatch.setattr(github_polling, "_parse_link_headers", slow_parse)
    base_url = "https://example.test"
    url = f"{base_url}/repos/owner/repo/issues/1/comments"
    link = ", ".join([_page_link(base_url, 2, "next"), _page_link(base_url, 2, "last")])
    session = _FakeSession(_FakeResponse([_comment()], link=link))

    with pytest.raises(asyncio.TimeoutError):
        await github_polling._fetch_comment_history(session, url, {})

    assert len(session.calls) == 1


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
