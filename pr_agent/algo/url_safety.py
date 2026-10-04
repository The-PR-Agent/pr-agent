"""SSRF guards for URLs PR-Agent fetches itself.

Two callers share this module: the Mosaico public-diff fetch and the `/ask` image
reachability probe. Only https URLs whose hostname resolves exclusively to public IPs
are allowed, and redirects are followed one hop at a time so every target is re-validated
instead of trusting the provider's own redirect handling.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

MAX_SAFE_REDIRECTS = 5
# Status codes the callers treat as a redirect; 300/304/305/306 are deliberately excluded
# because they do not carry a followable Location in the same way.
REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def ip_is_blocked(addr) -> bool:
    """Reject non-public IP ranges (SSRF guard): private/loopback/link-local (incl. cloud
    metadata 169.254.0.0/16), reserved, multicast, unspecified."""
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


async def host_resolves_public(host: str) -> bool:
    """True only if `host` resolves and EVERY resolved IP is public. DNS runs in a thread
    so it does not block the event loop. Any failure -> False (fail closed)."""
    if not host:
        return False
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except Exception:
        return False
    saw = False
    for info in infos:
        ip = info[4][0].split("%")[0]  # strip IPv6 zone id
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        saw = True
        if ip_is_blocked(addr):
            return False
    return saw


async def url_is_safe(url: str) -> bool:
    """SSRF gate for one URL: https scheme + a hostname that resolves only to public IPs."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    return await host_resolves_public(parsed.hostname)


async def with_safe_redirects(
    session,
    url: str,
    consumer,
    *,
    method: str = "GET",
    max_redirects: int = MAX_SAFE_REDIRECTS,
    validator=url_is_safe,
):
    """Run ``await consumer(response, url)`` on the first non-redirect hop for ``url``.

    Every hop, including the first URL, is validated with ``validator`` before it is
    requested, and each request uses ``allow_redirects=False``, so a validated URL is the
    only one contacted. Returns the consumer's result, or None when a hop is unsafe, a
    redirect carries no Location, or ``max_redirects`` is exceeded.
    """
    current = url
    for _ in range(max_redirects + 1):
        if not await validator(current):
            return None
        async with session.request(method, current, allow_redirects=False) as response:
            if response.status in REDIRECT_STATUSES:
                location = response.headers.get("Location")
                if not location:
                    return None
                current = urljoin(current, location)
                continue
            return await consumer(response, current)
    return None
