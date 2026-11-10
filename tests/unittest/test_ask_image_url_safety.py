"""Tests for the shared URL-safety helper and the /ask image reachability probe.

Covers the fix for #3829: the /ask image probe must be https-only and follow a bounded
number of redirects, re-validating every hop against the SSRF guard instead of letting
`requests.head` follow an unbounded redirect chain.
"""
import asyncio
import threading

import aiohttp
import pytest

from pr_agent.algo import url_safety
from pr_agent.algo.ai_handlers import litellm_ai_handler
from pr_agent.algo.ai_handlers.litellm_ai_handler import (
    _IMAGE_NOT_ALIVE_MESSAGE,
    LiteLLMAIHandler,
)
from pr_agent.algo.url_safety import MAX_SAFE_REDIRECTS, with_safe_redirects


class _Resp:
    def __init__(self, status, location=None):
        self.status = status
        self.headers = {"Location": location} if location else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requested = []

    def request(self, method, url, allow_redirects=True, **kwargs):
        self.requested.append((method, url, allow_redirects))
        self.request_options = kwargs
        return self._responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def _always_safe(url):
    return True


async def _never_safe(url):
    return False


async def _raise_consumer(response, url):
    raise AssertionError("consumer must not run")


# ---------------------------------------------------------------------------
# with_safe_redirects (shared by Mosaico and the /ask image probe)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unsafe_url_is_not_requested():
    session = _Session([])
    result = await with_safe_redirects(
        session, "http://internal.example/x", _raise_consumer, validator=_never_safe
    )
    assert result is None
    assert session.requested == []


@pytest.mark.asyncio
async def test_follows_validated_redirect_without_auto_redirects():
    session = _Session([_Resp(302, "https://github.com/next"), _Resp(200)])

    async def _consumer(response, url):
        return response.status

    result = await with_safe_redirects(
        session, "https://github.com/start", _consumer, validator=_always_safe
    )
    assert result == 200
    assert [url for _, url, _ in session.requested] == [
        "https://github.com/start",
        "https://github.com/next",
    ]
    # Every hop must be requested without automatic redirects so it can be validated first.
    assert all(allow_redirects is False for _, _, allow_redirects in session.requested)


@pytest.mark.asyncio
async def test_rejects_dns_rebinding_before_request(monkeypatch):
    session = _Session([_Resp(200)])

    monkeypatch.setattr(
        url_safety.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("10.0.0.9", 0))],
    )

    result = await with_safe_redirects(session, "https://example.com/start", lambda *_: None)

    assert result is None
    assert session.requested == []


@pytest.mark.asyncio
async def test_pins_request_to_validated_address(monkeypatch):
    session = _Session([_Resp(200)])

    async def consumer(response, url):
        return response.status

    monkeypatch.setattr(
        url_safety.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("140.82.121.4", 0))],
    )

    result = await with_safe_redirects(session, "https://example.com/start", consumer)

    assert result == 200
    assert str(session.requested[0][1]) == "https://140.82.121.4/start"
    assert session.requested[0][0] == "GET"
    assert session.requested[0][2] is False
    assert session.request_options == {"headers": {"Host": "example.com"}, "server_hostname": "example.com"}


@pytest.mark.asyncio
async def test_redirect_without_location_stops():
    session = _Session([_Resp(302)])
    result = await with_safe_redirects(
        session, "https://github.com/start", _raise_consumer, validator=_always_safe
    )
    assert result is None


@pytest.mark.asyncio
async def test_redirect_cap_is_enforced():
    session = _Session([_Resp(302, "https://github.com/loop")] * (MAX_SAFE_REDIRECTS + 2))
    result = await with_safe_redirects(
        session, "https://github.com/start", _raise_consumer, validator=_always_safe
    )
    assert result is None
    assert len(session.requested) == MAX_SAFE_REDIRECTS + 1


# ---------------------------------------------------------------------------
# LiteLLMAIHandler._image_url_error
# ---------------------------------------------------------------------------
def _patch_session(monkeypatch, responses):
    session = _Session(responses)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: session)
    return session


def _patch_public_dns(monkeypatch):
    monkeypatch.setattr(
        url_safety.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("140.82.121.4", 0))],
    )


@pytest.mark.asyncio
async def test_image_probe_rejects_non_https(monkeypatch):
    session = _patch_session(monkeypatch, [])

    error = await LiteLLMAIHandler._image_url_error("http://example.com/a.png")

    assert error == _IMAGE_NOT_ALIVE_MESSAGE
    assert session.requested == []


@pytest.mark.asyncio
async def test_image_probe_allows_reachable_https_image(monkeypatch):
    session = _patch_session(monkeypatch, [_Resp(200)])
    _patch_public_dns(monkeypatch)

    error = await LiteLLMAIHandler._image_url_error("https://example.com/a.png")

    assert error is None
    assert str(session.requested[0][1]) == "https://140.82.121.4/a.png"
    assert session.requested[0][0] == "HEAD"
    assert session.requested[0][2] is False


@pytest.mark.asyncio
async def test_image_probe_allows_forbidden_head(monkeypatch):
    # GitHub attachments redirect to S3 URLs signed for GET only, so HEAD answers 403.
    _patch_session(monkeypatch, [_Resp(403)])
    _patch_public_dns(monkeypatch)

    assert await LiteLLMAIHandler._image_url_error("https://example.com/a.png") is None


@pytest.mark.asyncio
async def test_image_probe_reports_missing_image(monkeypatch):
    _patch_session(monkeypatch, [_Resp(404)])
    _patch_public_dns(monkeypatch)

    error = await LiteLLMAIHandler._image_url_error("https://example.com/a.png")

    assert error == _IMAGE_NOT_ALIVE_MESSAGE


@pytest.mark.asyncio
async def test_image_probe_blocks_redirect_to_internal_host(monkeypatch):
    session = _patch_session(monkeypatch, [_Resp(302, "https://10.0.0.9/evil.png")])

    def fake_getaddrinfo(host, *a, **k):
        if host == "example.com":
            return [(2, 1, 6, "", ("140.82.121.4", 0))]
        return [(2, 1, 6, "", ("10.0.0.9", 0))]

    monkeypatch.setattr(url_safety.socket, "getaddrinfo", fake_getaddrinfo)

    error = await LiteLLMAIHandler._image_url_error("https://example.com/a.png")

    assert error == _IMAGE_NOT_ALIVE_MESSAGE
    # The unsafe second hop must never be requested.
    assert len(session.requested) == 1


@pytest.mark.asyncio
async def test_image_probe_deadline_covers_blocked_dns(monkeypatch):
    resolver_entered = threading.Event()
    resolver_release = threading.Event()
    resolver_done = threading.Event()

    def blocked_getaddrinfo(*args, **kwargs):
        resolver_entered.set()
        try:
            resolver_release.wait(2)
            return [(2, 1, 6, "", ("140.82.121.4", 0))]
        finally:
            resolver_done.set()

    class ObservedSession(_Session):
        exited = False
        cancelled = False

        async def __aexit__(self, exc_type, exc, tb):
            self.exited = True
            self.cancelled = exc_type is asyncio.CancelledError
            return await super().__aexit__(exc_type, exc, tb)

    session = ObservedSession([_Resp(200)])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: session)
    monkeypatch.setattr(url_safety.socket, "getaddrinfo", blocked_getaddrinfo)
    monkeypatch.setattr(litellm_ai_handler, "_IMAGE_HEAD_TIMEOUT_SECONDS", 0.05)

    try:
        # The longer watchdog bounds the unpatched baseline without becoming the oracle.
        async with asyncio.timeout(0.5):
            error = await LiteLLMAIHandler._image_url_error("https://example.com/a.png")
        assert resolver_entered.is_set()
        assert error == "Error fetching image: https://example.com/a.png"
        assert session.exited
        assert session.cancelled
        assert session.requested == []
        assert not resolver_done.is_set()
    finally:
        resolver_release.set()
        assert await asyncio.wait_for(asyncio.to_thread(resolver_done.wait, 1), 1.5)
        await asyncio.sleep(0)
    assert session.requested == []
