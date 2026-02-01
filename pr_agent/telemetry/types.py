from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class TelemetryConfig:
    enabled: bool
    exporter_type: str
    service_name: str
    service_version: str
    environment: str
    otlp_endpoint: Optional[str]
    otlp_headers: Optional[Dict[str, str]]
