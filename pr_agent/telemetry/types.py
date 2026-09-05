from dataclasses import dataclass
from typing import Dict, Optional


class ExporterType:
    """Canonical exporter type values for OTEL.EXPORTER_TYPE."""
    OTLP = "otlp"
    CONSOLE = "console"
    NONE = "none"


class OtlpProtocol:
    """Canonical transport values for OTEL.OTLP_PROTOCOL."""
    HTTP = "http"
    GRPC = "grpc"  # exporter is the optional otel-grpc extra


@dataclass
class TelemetryConfig:
    is_enabled: bool = False
    exporter_type: Optional[str] = None
    service_name: Optional[str] = None
    service_version: Optional[str] = None
    environment: Optional[str] = None
    otlp_endpoint: Optional[str] = None
    otlp_headers: Optional[Dict[str, str]] = None
    otlp_timeout: int = 3  # seconds; hard deadline per export call, retries included
    otlp_protocol: str = OtlpProtocol.HTTP
