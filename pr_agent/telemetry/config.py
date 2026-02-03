from pr_agent.algo.utils import get_version
from pr_agent.config_loader import get_settings
from pr_agent.telemetry.types import TelemetryConfig


def get_otel_config() -> TelemetryConfig:
    """Read and validate telemetry configuration from settings"""
    settings = get_settings()

    # Check if telemetry is is_enabled
    is_enabled = settings.get("OTEL.is_enabled", True)
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
    exporter_type = settings.get("OTEL.EXPORTER_TYPE", None)
    service_name = settings.get("OTEL.SERVICE_NAME", None)
    service_version = get_version()  # From pr_agent.algo.utils
    environment = settings.get("OTEL.ENVIRONMENT", None)

    # OTLP configuration (secrets)
    otlp_endpoint = settings.get("OTEL.OTLP_ENDPOINT")
    otlp_headers_raw = settings.get("OTEL.OTLP_HEADERS")
    otlp_headers = _parse_otlp_headers(otlp_headers_raw) if otlp_headers_raw else None

    # Validate OTLP configuration if using OTLP exporter
    if exporter_type == "otlp":
        if not otlp_endpoint:
            raise ValueError(
                "OTEL.OTLP_ENDPOINT must be configured in secrets.toml when "
                "OTEL.EXPORTER_TYPE is set to 'otlp'. Please add the endpoint URL "
                "to your secrets.toml file."
            )

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