from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class TelemetryConfig:
    is_enabled: bool
    exporter_type: Optional[str]
    service_name: Optional[str]
    service_version: Optional[str]
    environment: Optional[str]
    otlp_endpoint: Optional[str]
    otlp_headers: Optional[Dict[str, str]]
