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

from yarl import URL

MAX_SAFE_REDIRECTS = 5
# Status codes the callers treat as a redirect; 300/304/305/306 are deliberately excluded
# because they do not carry a followable Location in the same way.
REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def ip_is_blocked(addr) -> bool:
    """Reject non-public IP ranges, including shared/CGNAT and site-local addresses."""
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified
            or not addr.is_global or getattr(addr, "is_site_local", False))


async def host_resolves_public(host: str) -> bool:
    """True only if `host` resolves and EVERY resolved IP is public. DNS runs in a thread
    so it does not block the event loop. Any failure -> False (fail closed)."""
    if not host:
        return False
    try:
        infos = await _resolve_public_addresses(host)
    except (OSError, ValueError):
        return False

    return bool(infos)


async def _resolve_public_addresses(host: str, port: int | None = None) -> list:
    """Resolve a host once and return only after every address is confirmed public."""
    infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    if not infos:
        return []
    for info in infos:
        ip = info[4][0].split("%")[0]
        try:
            addr = ipaddress.ip_address(ip)
        except (IndexError, ValueError) as error:
            raise ValueError("DNS returned an invalid address") from error
        if ip_is_blocked(addr):
            raise ValueError("DNS returned a non-public address")
    return infos


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
    requested. Built-in validation pins the request to a validated public address while
    preserving the original Host header and TLS hostname. Each request uses
    ``allow_redirects=False``, so redirects are validated one hop at a time. Returns the
    consumer's result, or None when a hop is unsafe, a redirect carries no Location, or
    ``max_redirects`` is exceeded.
    """
    current = url
    for _ in range(max_redirects + 1):
        request_url = current
        request_options = {"allow_redirects": False}
        if validator is url_is_safe:
            parsed = URL(current)
            if parsed.scheme != "https" or not parsed.raw_host:
                return None
            try:
                addresses = await _resolve_public_addresses(parsed.raw_host, parsed.port)
            except (OSError, ValueError):
                return None
            if not addresses:
                return None
            request_url = parsed.with_host(addresses[0][4][0])
            request_options["headers"] = {"Host": parsed.raw_authority.rsplit("@", 1)[-1]}
            if parsed.scheme == "https":
                request_options["server_hostname"] = parsed.raw_host
        elif not await validator(current):
            return None
        async with session.request(method, request_url, **request_options) as response:
            if response.status in REDIRECT_STATUSES:
                location = response.headers.get("Location")
                if not location:
                    return None
                current = urljoin(current, location)
                continue
            return await consumer(response, current)
    return None
