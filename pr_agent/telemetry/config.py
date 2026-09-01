from pr_agent.algo.utils import get_version
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_agent.telemetry.types import ExporterType, OtlpProtocol, TelemetryConfig

VALID_EXPORTER_TYPES = {ExporterType.CONSOLE, ExporterType.OTLP, ExporterType.NONE}
VALID_OTLP_PROTOCOLS = {OtlpProtocol.HTTP, OtlpProtocol.GRPC}


def get_otel_config() -> TelemetryConfig:
    """Read and validate telemetry configuration from settings"""
    settings = get_settings()
    if not settings.get("OTEL.IS_ENABLED", False):
        return TelemetryConfig()

    exporter_type = settings.get("OTEL.EXPORTER_TYPE", None)
    service_name = settings.get("OTEL.SERVICE_NAME", None)
    environment = settings.get("OTEL.ENVIRONMENT", None)
    # Endpoint and headers are secrets — never included in log messages
    otlp_endpoint = settings.get("OTEL.OTLP_ENDPOINT")
    otlp_headers_raw = settings.get("OTEL.OTLP_HEADERS")
    otlp_protocol = str(settings.get("OTEL.OTLP_PROTOCOL", OtlpProtocol.HTTP)).strip().lower()

    # Validate closed-set values before the fallbacks below can absorb a typo
    if exporter_type and exporter_type not in VALID_EXPORTER_TYPES:
        raise ValueError(
            f"Invalid OTEL.EXPORTER_TYPE '{exporter_type}'. "
            f"Valid options are: {', '.join(sorted(VALID_EXPORTER_TYPES))}"
        )
    if otlp_protocol not in VALID_OTLP_PROTOCOLS:
        raise ValueError(
            f"Invalid OTEL.OTLP_PROTOCOL '{otlp_protocol}'. "
            f"Valid options are: {', '.join(sorted(VALID_OTLP_PROTOCOLS))}"
        )

    if not (exporter_type and service_name and environment):
        get_logger().warning(
            f"OpenTelemetry enabled but missing required configuration - "
            f"exporter_type: {exporter_type}, service_name: {service_name}, "
            f"environment: {environment}. Falling back to non-OTEL mode."
        )
        return TelemetryConfig()

    # Fail closed: opted-in telemetry content was consented for the OTLP destination
    # only — never redirect it to another exporter (console = process logs).
    if exporter_type == ExporterType.OTLP and not otlp_endpoint:
        get_logger().warning(
            "OTEL.EXPORTER_TYPE is 'otlp' but OTEL.OTLP_ENDPOINT is not configured. "
            "Telemetry disabled — not falling back to another exporter."
        )
        return TelemetryConfig()

    return TelemetryConfig(
        is_enabled=True,
        exporter_type=exporter_type,
        service_name=service_name,
        service_version=get_version(),
        environment=environment,
        otlp_endpoint=otlp_endpoint,
        otlp_headers=_parse_otlp_headers(otlp_headers_raw) if otlp_headers_raw else None,
        otlp_timeout=int(settings.get("OTEL.OTLP_TIMEOUT", 3)),
        otlp_protocol=otlp_protocol
    )


def otlp_signal_endpoint(endpoint: str, signal: str) -> str:
    """Append the "/v1/traces" or "/v1/metrics" path OTLP/HTTP requires.

    Unlike the OTEL_EXPORTER_OTLP_ENDPOINT env var, the HTTP exporter uses its
    `endpoint` argument verbatim; skipped when the caller already supplied it.
    """
    endpoint = endpoint.rstrip('/')
    suffix = f'/v1/{signal}'
    return endpoint if endpoint.endswith(suffix) else endpoint + suffix


def _parse_otlp_headers(headers_str: str) -> dict[str, str]:
    """Parse "key1=value1,key2=value2" into a dict."""
    if not headers_str or not headers_str.strip():
        return {}

    headers = {}
    for pair in headers_str.split(','):
        pair = pair.strip()
        if '=' in pair:
            key, value = pair.split('=', 1)
            headers[key.strip()] = value.strip()

    return headers
