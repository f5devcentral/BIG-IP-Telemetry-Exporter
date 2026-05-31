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


VALIDATION_PROMETHEUS_ENDPOINT = "0.0.0.0:8889"


def _prometheus_endpoint(params: dict[str, Any]) -> str:
    return str(params.get("endpoint", VALIDATION_PROMETHEUS_ENDPOINT)).strip()


def _skips_duplicate_validation_prometheus(
    etype: str,
    params: dict[str, Any],
    *,
    include_validation_prometheus: bool,
) -> bool:
    """The validation exporter already binds :8889; skip a second prometheus on the same port."""
    if not include_validation_prometheus or etype != "prometheus":
        return False
    endpoint = _prometheus_endpoint(params)
    return endpoint in {VALIDATION_PROMETHEUS_ENDPOINT, ":8889", "8889"}


def _append_pipeline_exporters(
    items: list[dict[str, Any]],
    *,
    pipeline: str,
    exporters: dict[str, Any],
    pipeline_keys: list[str],
    include_validation_prometheus: bool = False,
) -> None:
    """Merge enabled exporter selections into the collector config for one pipeline."""
    for idx, item in enumerate(items):
        if not item.get("enabled", True):
            continue
        etype = item["type"]
        params = {**(item.get("params") or {}), "_pipeline": pipeline}
        if _skips_duplicate_validation_prometheus(
            etype,
            params,
            include_validation_prometheus=include_validation_prometheus,
        ):
            continue
        component, block = resolve_exporter(etype, params)
        prefix = "m" if pipeline == "metrics" else "l"
        key = f"{component}/{prefix}{idx}"
        exporters[key] = block
        pipeline_keys.append(key)


def _has_enabled_exporters(items: list[dict[str, Any]]) -> bool:
    return any(item.get("enabled", True) for item in items)


def build_collector_config(
    metric_exporters: list[dict[str, Any]],
    log_exporters: list[dict[str, Any]] | None = None,
    *,
    export_metrics: bool = True,
    export_logs: bool = True,
    include_validation_prometheus: bool = True,
) -> dict[str, Any]:
    """
    Build collector YAML from separate metric and log exporter selections.

    metric_exporters / log_exporters: [{type, params, enabled}]
    Pipelines are included when the export mode is enabled or when exporters
    are configured for that signal (so tcplog/syslog listeners stay up).
    """
    log_exporters = log_exporters if log_exporters is not None else []
    export_metrics = export_metrics or _has_enabled_exporters(metric_exporters)
    export_logs = export_logs or _has_enabled_exporters(log_exporters)

    exporters: dict[str, Any] = {}
    metric_pipeline_keys: list[str] = []
    log_pipeline_keys: list[str] = []

    if export_metrics:
        if include_validation_prometheus:
            _, prom_block = resolve_exporter("prometheus", {"endpoint": "0.0.0.0:8889"})
            exporters["prometheus/validation"] = prom_block
            metric_pipeline_keys.append("prometheus/validation")

        _append_pipeline_exporters(
            metric_exporters,
            pipeline="metrics",
            exporters=exporters,
            pipeline_keys=metric_pipeline_keys,
            include_validation_prometheus=include_validation_prometheus,
        )

        if not metric_pipeline_keys:
            _, prom_block = resolve_exporter("prometheus", {})
            exporters["prometheus/validation"] = prom_block
            metric_pipeline_keys.append("prometheus/validation")

    if export_logs:
        _append_pipeline_exporters(
            log_exporters,
            pipeline="logs",
            exporters=exporters,
            pipeline_keys=log_pipeline_keys,
        )
        if not log_pipeline_keys:
            exporters["debug/logs"] = {"verbosity": "basic"}
            log_pipeline_keys.append("debug/logs")

    receivers: dict[str, Any] = {}
    pipelines: dict[str, Any] = {}

    if export_metrics:
        receivers["otlp"] = {
            "protocols": {
                "grpc": {"endpoint": "0.0.0.0:4317"},
                "http": {"endpoint": "0.0.0.0:4318"},
            }
        }
        pipelines["metrics"] = {
            "receivers": ["otlp"],
            "processors": ["memory_limiter", "batch"],
            "exporters": metric_pipeline_keys,
        }

    if export_logs:
        receivers["tcplog"] = {
            "listen_address": f"0.0.0.0:{os.environ.get('BIGIP_LOG_HSL_PORT', '5141')}",
        }
        receivers["syslog"] = {
            "tcp": {
                "listen_address": f"0.0.0.0:{os.environ.get('BIGIP_LOG_SYSLOG_PORT', '5140')}",
            },
            "protocol": "rfc5424",
        }
        pipelines["logs"] = {
            "receivers": ["syslog", "tcplog"],
            "processors": ["memory_limiter", "batch"],
            "exporters": log_pipeline_keys,
        }

    config: dict[str, Any] = {
        "receivers": receivers,
        "processors": {
            "batch": {"timeout": "5s", "send_batch_size": 512},
            "memory_limiter": {
                "check_interval": "1s",
                "limit_mib": 512,
                "spike_limit_mib": 128,
            },
        },
        "exporters": exporters,
        "extensions": {"health_check": {"endpoint": "0.0.0.0:13133"}},
        "service": {
            "extensions": ["health_check"],
            "pipelines": pipelines,
            "telemetry": {
                "logs": {"level": "info"},
                "metrics": {"level": "basic"},
            },
        },
    }
    return config


def build_collector_config_legacy(
    selected_exporters: list[dict[str, Any]],
    *,
    include_validation_prometheus: bool = True,
) -> dict[str, Any]:
    """Back-compat wrapper: all exporters go to the metrics pipeline."""
    return build_collector_config(
        selected_exporters,
        [],
        export_metrics=True,
        export_logs=True,
        include_validation_prometheus=include_validation_prometheus,
    )


def write_collector_config(
    metric_exporters: list[dict[str, Any]],
    log_exporters: list[dict[str, Any]] | None = None,
    *,
    export_metrics: bool = True,
    export_logs: bool = True,
    path: Path | None = None,
) -> Path:
    path = path or GENERATED_CONFIG_PATH
    config = build_collector_config(
        metric_exporters,
        log_exporters,
        export_metrics=export_metrics,
        export_logs=export_logs,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False, default_flow_style=False)
    return path


__all__ = [
    "EXPORTER_TYPES",
    "CONTRIB_EXPORTERS_REPO",
    "build_collector_config",
    "build_collector_config_legacy",
    "write_collector_config",
    "list_exporter_catalog",
    "list_contrib_components",
]
