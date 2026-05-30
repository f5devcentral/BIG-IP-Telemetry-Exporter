"""Generate OpenTelemetry Collector YAML from UI exporter selections."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from backend.contrib_exporters import (
    CONTRIB_EXPORTERS_REPO,
    EXPORTER_TYPES,
    list_contrib_components,
    list_exporter_catalog,
    resolve_exporter,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "otel-collector" / "config.yaml"
GENERATED_CONFIG_PATH = Path(
    os.environ.get("COLLECTOR_CONFIG_PATH", str(REPO_ROOT / "otel-collector" / "generated-config.yaml")),
)


def build_collector_config(
    selected_exporters: list[dict[str, Any]],
    *,
    include_validation_prometheus: bool = True,
) -> dict[str, Any]:
    """
    selected_exporters: [{type, params, enabled}]
    Always includes OTLP receivers for the Python backend.
    """
    exporters: dict[str, Any] = {}
    metric_exporters: list[str] = []

    if include_validation_prometheus:
        _, prom_block = resolve_exporter("prometheus", {"endpoint": "0.0.0.0:8889"})
        exporters["prometheus/validation"] = prom_block
        metric_exporters.append("prometheus/validation")

    for idx, item in enumerate(selected_exporters):
        if not item.get("enabled", True):
            continue
        etype = item["type"]
        params = item.get("params") or {}
        if etype == "prometheus" and include_validation_prometheus:
            # Avoid duplicate validation exporter; user prometheus adds extra instance
            component, block = resolve_exporter(etype, params)
            key = f"{component}/user{idx}"
        else:
            component, block = resolve_exporter(etype, params)
            key = f"{component}/{idx}"
        exporters[key] = block
        metric_exporters.append(key)

    if not metric_exporters:
        _, prom_block = resolve_exporter("prometheus", {})
        exporters["prometheus/validation"] = prom_block
        metric_exporters.append("prometheus/validation")

    config = {
        "receivers": {
            "otlp": {
                "protocols": {
                    "grpc": {"endpoint": "0.0.0.0:4317"},
                    "http": {"endpoint": "0.0.0.0:4318"},
                }
            },
            "tcplog": {
                "listen_address": f"0.0.0.0:{os.environ.get('BIGIP_LOG_HSL_PORT', '5141')}",
            },
            "syslog": {
                "tcp": {
                    "listen_address": f"0.0.0.0:{os.environ.get('BIGIP_LOG_SYSLOG_PORT', '5140')}",
                },
                "protocol": "rfc5424",
            },
        },
        "processors": {
            "batch": {"timeout": "5s", "send_batch_size": 512},
            "memory_limiter": {
                "check_interval": "1s",
                "limit_mib": 512,
                "spike_limit_mib": 128,
            },
        },
        "exporters": {
            **exporters,
            "debug/logs": {"verbosity": "basic"},
        },
        "extensions": {"health_check": {"endpoint": "0.0.0.0:13133"}},
        "service": {
            "extensions": ["health_check"],
            "pipelines": {
                "metrics": {
                    "receivers": ["otlp"],
                    "processors": ["memory_limiter", "batch"],
                    "exporters": metric_exporters,
                },
                "logs": {
                    "receivers": ["syslog", "tcplog"],
                    "processors": ["memory_limiter", "batch"],
                    "exporters": ["debug/logs"],
                },
            },
            "telemetry": {
                "logs": {"level": "info"},
                "metrics": {"level": "basic"},
            },
        },
    }
    return config


def write_collector_config(
    selected_exporters: list[dict[str, Any]],
    *,
    path: Path | None = None,
) -> Path:
    path = path or GENERATED_CONFIG_PATH
    config = build_collector_config(selected_exporters)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False, default_flow_style=False)
    return path


__all__ = [
    "EXPORTER_TYPES",
    "CONTRIB_EXPORTERS_REPO",
    "build_collector_config",
    "write_collector_config",
    "list_exporter_catalog",
    "list_contrib_components",
]
