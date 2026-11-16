import math

from ..config_loader import get_settings
from ..log import get_logger

DEFAULT_HTTP_REQUEST_TIMEOUT = 60.0
MAX_HTTP_REQUEST_TIMEOUT = 600.0


def get_http_request_timeout() -> float:
    """Return the host timeout in seconds, defaulting invalid values and capping large ones."""
    value = get_settings().get("config.http_request_timeout", DEFAULT_HTTP_REQUEST_TIMEOUT)
    try:
        timeout = 0.0 if isinstance(value, bool) else float(value)
    except (TypeError, ValueError, OverflowError):
        timeout = 0.0
    if not math.isfinite(timeout) or timeout <= 0:
        get_logger().warning(f"Ignoring invalid config.http_request_timeout, using {DEFAULT_HTTP_REQUEST_TIMEOUT:g}")
        return DEFAULT_HTTP_REQUEST_TIMEOUT
    if timeout > MAX_HTTP_REQUEST_TIMEOUT:
        get_logger().warning(f"config.http_request_timeout is above {MAX_HTTP_REQUEST_TIMEOUT:g}, using the ceiling")
        return MAX_HTTP_REQUEST_TIMEOUT
    return timeout


def refresh_session_request_timeout(client) -> None:
    """Re-read the host timeout on every GitLab request so cached clients follow host settings."""
    get_options = client._get_session_opts

    def options_with_timeout():
        return {**get_options(), "timeout": get_http_request_timeout()}

    client._get_session_opts = options_with_timeout
