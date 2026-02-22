from pr_agent.algo.utils import get_version
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_agent.telemetry.types import TelemetryConfig

VALID_EXPORTER_TYPES = {"console", "otlp", "none"}

def get_otel_config() -> TelemetryConfig:
    """Read and validate telemetry configuration from settings"""
    # Check if telemetry is is_enabled
    is_enabled = get_settings().get("OTEL.IS_ENABLED", False)
    if not is_enabled:
        return TelemetryConfig(
            is_enabled=False,
            exporter_type=None,
            service_name=None,
            service_version=None,
            environment=None,
            otlp_endpoint=None,
            otlp_headers=None
        )

    # Read configuration from settings
    exporter_type = get_settings().get("OTEL.EXPORTER_TYPE", None)
    service_name = get_settings().get("OTEL.SERVICE_NAME", None)
    service_version = get_version()  # From pr_agent.algo.utils
    environment = get_settings().get("OTEL.ENVIRONMENT", None)

    # OTLP configuration (secrets — intentionally not included in any log messages)
    otlp_endpoint = get_settings().get("OTEL.OTLP_ENDPOINT")
    otlp_headers_raw = get_settings().get("OTEL.OTLP_HEADERS")
    otlp_headers = _parse_otlp_headers(otlp_headers_raw) if otlp_headers_raw else None

    # Validate exporter type early — before missing-fields check so it is always surfaced
    if exporter_type and exporter_type not in VALID_EXPORTER_TYPES:
        raise ValueError(
            f"Invalid OTEL.EXPORTER_TYPE '{exporter_type}'. "
            f"Valid options are: {', '.join(sorted(VALID_EXPORTER_TYPES))}"
        )

    # Validate required fields - fall back to disabled if missing
    if not (exporter_type and service_name and environment):
        get_logger().warning(
            f"OpenTelemetry enabled but missing required configuration - "
            f"exporter_type: {exporter_type}, service_name: {service_name}, "
            f"environment: {environment}. Falling back to non-OTEL mode."
        )
        return TelemetryConfig(
            is_enabled=False,
            exporter_type=exporter_type,
            service_name=service_name,
            service_version=service_version,
            environment=environment,
            otlp_endpoint=otlp_endpoint,
            otlp_headers=otlp_headers
        )

    # Validate OTLP configuration if using OTLP exporter
    if exporter_type == "otlp" and not otlp_endpoint:
        get_logger().warning(
            "OTEL.EXPORTER_TYPE is 'otlp' but OTEL.OTLP_ENDPOINT is not configured. "
            "Falling back to 'console' exporter."
        )
        exporter_type = "console"

    return TelemetryConfig(
        is_enabled=True,
        exporter_type=exporter_type,
        service_name=service_name,
        service_version=service_version,
        environment=environment,
        otlp_endpoint=otlp_endpoint,
        otlp_headers=otlp_headers
    )


def _parse_otlp_headers(headers_str: str) -> dict[str, str]:
    """
    Parse OTLP headers from configuration string.

    Format: "key1=value1,key2=value2" or "key1=value1"
    Example: "x-honeycomb-team=YOUR_API_KEY" or "Authorization=Bearer TOKEN,x-custom=value"
    """
    if not headers_str or not headers_str.strip():
        return {}

    headers = {}
    for pair in headers_str.split(','):
        pair = pair.strip()
        if '=' in pair:
            key, value = pair.split('=', 1)
            headers[key.strip()] = value.strip()

    return headers