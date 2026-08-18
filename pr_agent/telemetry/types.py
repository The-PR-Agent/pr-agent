from dataclasses import dataclass
from typing import Dict, Optional


class ExporterType:
    """Canonical exporter type values for OTEL.EXPORTER_TYPE."""
    OTLP = "otlp"
    CONSOLE = "console"
    NONE = "none"


@dataclass
class TelemetryConfig:
    is_enabled: bool
    exporter_type: Optional[str]
    service_name: Optional[str]
    service_version: Optional[str]
    environment: Optional[str]
    otlp_endpoint: Optional[str]
    otlp_headers: Optional[Dict[str, str]]
