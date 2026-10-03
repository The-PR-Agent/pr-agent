"""The request timeout PR-Agent sends to a git provider, and the bounds on reading it.

Not an HTTP client: the clients belong to the SDKs. PyGithub, boto3 and the Bitbucket SDK pick
their own timeouts, but python-gitlab defaults to none, giteapy's generated methods forward one as
None unless a caller supplied a value, and the requests calls the Bitbucket and Gerrit providers
make had none at all. A stalled connection to a self-hosted host would then block a worker
forever, and the webhook servers run these calls on the event loop, so one hung socket freezes
everything else that worker was serving.
"""

import hashlib
import math

from ..config_loader import get_settings
from ..log import get_logger

DEFAULT_HTTP_REQUEST_TIMEOUT = 60.0  # seconds of connect and read inactivity per HTTP request
# A ceiling keeps the bound in place when a repository overrides this in its own .pr_agent.toml,
# so a per-repository value cannot undo the bound.
MAX_HTTP_REQUEST_TIMEOUT = 600.0
# Bounded because the key comes from a repository-controlled setting, which a long-lived server
# reads once per revision; without a ceiling, every value it ever produced would be remembered.
_reported_timeouts: set[str] = set()
_MAX_REPORTED_TIMEOUTS = 64
# the prefix keeps a digest recognisable if the set is ever dumped next to the values themselves
_REPORT_KEY_PREFIX = "sha256:"


def get_http_request_timeout() -> float:
    """Return the configured timeout for the git provider clients.

    An unusable value falls back to the default rather than raising, and an oversized one is
    capped, so a typo or a repository override still leaves the clients bounded. The Gitea and
    the requests-based clients read this per call; the GitLab client takes it when it is built,
    and the one apply_repo_settings builds for a command is built before that repository's own
    settings are merged, so a per-repository value does not reach that client.
    """
    value = get_settings().get("config.http_request_timeout", DEFAULT_HTTP_REQUEST_TIMEOUT)
    if isinstance(value, bool):
        timeout = 0.0  # a bool is not a duration, and True would read as one second
    else:
        try:
            timeout = float(value)
        except (TypeError, ValueError, OverflowError):
            timeout = 0.0
    if not math.isfinite(timeout) or timeout <= 0:
        return _report_once(
            _report_key(value),
            f"Ignoring config.http_request_timeout ({_rendered(value)}); it must be a positive, "
            f"finite number of seconds, so using {DEFAULT_HTTP_REQUEST_TIMEOUT:g}",
            DEFAULT_HTTP_REQUEST_TIMEOUT,
        )
    if timeout > MAX_HTTP_REQUEST_TIMEOUT:
        return _report_once(
            _report_key(value),
            f"config.http_request_timeout ({_rendered(value)}) is above the "
            f"{MAX_HTTP_REQUEST_TIMEOUT:g} second ceiling, so using the ceiling",
            MAX_HTTP_REQUEST_TIMEOUT,
        )
    return timeout


def _report_key(value) -> str:
    """Digest ``value`` into a fixed-length key for the already-reported set.

    Digested rather than truncated: a repository can put an arbitrarily long string here, and two
    values sharing their first 64 characters are still two values worth telling apart.
    """
    return _REPORT_KEY_PREFIX + hashlib.sha256(
        repr(value).encode("utf-8", "replace")).hexdigest()


def _rendered(value) -> str:
    """Render ``value`` for a log line, bounded because it comes from a repository setting."""
    rendered = repr(value)
    return rendered if len(rendered) <= 120 else f"{rendered[:117]}..."


def _report_once(key: str, message: str, fallback: float) -> float:
    """Log ``message`` unless this key was reported recently, then return ``fallback``.

    The memory of what was reported is capped, so a value that recurs after enough others have
    been seen is reported again rather than silently kept out of the log forever.
    """
    if key not in _reported_timeouts:
        if len(_reported_timeouts) >= _MAX_REPORTED_TIMEOUTS:
            _reported_timeouts.clear()  # a long-lived server would otherwise keep them forever
        _reported_timeouts.add(key)
        get_logger().warning(message)
    return fallback
