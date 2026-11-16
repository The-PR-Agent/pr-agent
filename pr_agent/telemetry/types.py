from dataclasses import dataclass


class ExporterType:
    """Canonical exporter type values for OTEL.EXPORTER_TYPE."""
    OTLP = "otlp"
    CONSOLE = "console"
    PROMETHEUS = "prometheus"
    NONE = "none"


class OtlpProtocol:
    """Canonical transport values for OTEL.OTLP_PROTOCOL."""
    HTTP = "http"
    GRPC = "grpc"  # exporter is the optional otel-grpc extra


@dataclass
class TelemetryConfig:
    is_enabled: bool = False
    exporter_type: str | None = None
    service_name: str | None = None
    service_version: str | None = None
    environment: str | None = None
    otlp_endpoint: str | None = None
    otlp_headers: dict[str, str] | None = None
    otlp_timeout: int = 3  # seconds; hard deadline per export call, retries included
    otlp_protocol: str = OtlpProtocol.HTTP
    prometheus_multiproc_dir: str | None = None  # shared dir for multiprocess metrics
