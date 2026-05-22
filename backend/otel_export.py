"""Push extracted metrics to an OTLP HTTP endpoint (OpenTelemetry Collector)."""

from __future__ import annotations

import time
from typing import Any

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

# (display host, session_id, client)
BigIPClientEntry = tuple[str, str, Any]


class OTLPMetricsPusher:
    def __init__(self, endpoint: str, *, interval_ms: int = 5000) -> None:
        endpoint = endpoint.rstrip("/")
        if not endpoint.endswith("/v1/metrics"):
            endpoint = f"{endpoint}/v1/metrics"
        self._endpoint = endpoint.rstrip("/").removesuffix("/v1/metrics")
        resource = Resource.create(
            {
                "service.name": "bigip-metrics-exporter",
                "service.namespace": "f5",
            }
        )
        exporter = OTLPMetricExporter(endpoint=endpoint)
        reader = PeriodicExportingMetricReader(
            exporter,
            export_interval_millis=interval_ms,
        )
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(provider)
        self._meter = metrics.get_meter("bigip.metrics")
        self._instruments: dict[str, Any] = {}
        self._provider = provider

    def record_batch(self, points: list[dict[str, Any]]) -> int:
        count = 0
        for pt in points:
            host = pt.get("attributes", {}).get("bigip.host", "unknown")
            name = pt["name"]
            key = f"{host}::{name}"
            if key not in self._instruments:
                safe = name.replace(".", "_")[:100]
                self._instruments[key] = self._meter.create_up_down_counter(
                    safe,
                    description=f"BIG-IP metric {name} ({host})",
                )
            inst = self._instruments[key]
            value = pt["value"]
            inst.add(int(value) if float(value).is_integer() else value)
            count += 1
        return count

    def force_flush(self, timeout_ms: int = 10000) -> bool:
        return self._provider.force_flush(timeout_millis=timeout_ms)

    def shutdown(self) -> None:
        self._provider.shutdown()


class MetricsExportLoop:
    def __init__(
        self,
        clients: list[BigIPClientEntry],
        endpoints: list[str],
        pusher: OTLPMetricsPusher,
        *,
        poll_interval_sec: float = 30.0,
    ) -> None:
        self._clients = clients
        self._endpoints = endpoints
        self._pusher = pusher
        self._poll_interval_sec = poll_interval_sec
        self._running = False
        self._last_run: float | None = None
        self._last_error: str | None = None
        self._last_point_count = 0
        self._last_errors_by_host: dict[str, list[str]] = {}

    @property
    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "endpoints": len(self._endpoints),
            "bigip_count": len(self._clients),
            "bigip_hosts": [h for h, _, _ in self._clients],
            "last_run": self._last_run,
            "last_error": self._last_error,
            "last_point_count": self._last_point_count,
            "last_errors_by_host": self._last_errors_by_host,
            "poll_interval_sec": self._poll_interval_sec,
        }

    def run_once(self) -> dict[str, Any]:
        from .metrics_extractor import extract_metrics

        total = 0
        errors: list[str] = []
        errors_by_host: dict[str, list[str]] = {}

        for host, _sid, client in self._clients:
            host_errors: list[str] = []
            for ep in self._endpoints:
                try:
                    payload = client.get(ep)
                    points = extract_metrics(ep, payload, bigip_host=host)
                    total += self._pusher.record_batch(points)
                except Exception as exc:  # noqa: BLE001
                    msg = f"{ep}: {exc}"
                    errors.append(f"[{host}] {msg}")
                    host_errors.append(msg)
            if host_errors:
                errors_by_host[host] = host_errors[:10]

        self._pusher.force_flush()
        self._last_run = time.time()
        self._last_point_count = total
        self._last_errors_by_host = errors_by_host
        self._last_error = "; ".join(errors[:8]) if errors else None
        return {
            "points": total,
            "errors": errors,
            "errors_by_host": errors_by_host,
        }

    def start_background(self) -> None:
        import threading

        if self._running:
            return
        self._running = True

        def _loop() -> None:
            while self._running:
                try:
                    self.run_once()
                except Exception as exc:  # noqa: BLE001
                    self._last_error = str(exc)
                time.sleep(self._poll_interval_sec)

        t = threading.Thread(target=_loop, name="bigip-export-loop", daemon=True)
        t.start()

    def stop(self) -> None:
        self._running = False
