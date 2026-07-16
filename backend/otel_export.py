"""Push extracted metrics to an OTLP HTTP endpoint (OpenTelemetry Collector)."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import requests
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

logger = logging.getLogger(__name__)

# (display host, session_id, client)
BigIPClientEntry = tuple[str, str, Any]
EndpointResolver = Callable[[str], list[str]]
TmctlResolver = Callable[[str], list[str]]


class OTLPMetricsPusher:
    def __init__(self, endpoint: str, *, interval_ms: int = 5000) -> None:
        endpoint = endpoint.rstrip("/")
        if not endpoint.endswith("/v1/metrics"):
            endpoint = f"{endpoint}/v1/metrics"
        self._endpoint = endpoint.rstrip("/").removesuffix("/v1/metrics")
        resource = Resource.create(
            {
                "service.name": "bigip-telemetry-exporter",
                "service.namespace": "f5",
            }
        )
        # Avoid corporate HTTP(S)_PROXY hijacking localhost OTLP to the collector.
        session = requests.Session()
        session.trust_env = False
        exporter = OTLPMetricExporter(endpoint=endpoint, session=session)
        reader = PeriodicExportingMetricReader(
            exporter,
            export_interval_millis=interval_ms,
        )
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        # Use the provider directly. Do NOT call metrics.set_meter_provider():
        # OpenTelemetry forbids replacing the global provider, so stop/start or
        # nuke leaves record_batch writing to a shutdown MeterProvider while
        # force_flush on a new (empty) provider "succeeds" with no datapoints.
        self._provider = provider
        self._meter = provider.get_meter("bigip.metrics")
        self._instruments: dict[str, Any] = {}
        self._last_flush_ok: bool | None = None

    @staticmethod
    def _otel_attributes(raw: dict[str, Any]) -> dict[str, str]:
        """OTLP/Prometheus labels: string attributes on each data point."""
        out: dict[str, str] = {}
        for key, val in raw.items():
            if val is None:
                continue
            out[str(key)] = str(val)
        return out

    def record_batch(self, points: list[dict[str, Any]]) -> int:
        count = 0
        for pt in points:
            name = pt["name"]
            safe = re.sub(r"[^a-zA-Z0-9_]", "_", name)[:255]
            if safe not in self._instruments:
                host = pt.get("attributes", {}).get("bigip.host", "unknown")
                self._instruments[safe] = self._meter.create_gauge(
                    safe,
                    description=f"BIG-IP metric {name} ({host})",
                )
            inst = self._instruments[safe]
            value = pt["value"]
            attrs = self._otel_attributes(pt.get("attributes") or {})
            numeric = int(value) if float(value).is_integer() else float(value)
            inst.set(numeric, attributes=attrs)
            count += 1
        return count

    def force_flush(self, timeout_ms: int = 10000) -> bool:
        ok = bool(self._provider.force_flush(timeout_millis=timeout_ms))
        self._last_flush_ok = ok
        if not ok:
            logger.warning(
                "OTLP force_flush failed for endpoint %s (metrics may not reach collector)",
                self._endpoint,
            )
        return ok

    def shutdown(self) -> None:
        self._provider.shutdown()

    @property
    def status(self) -> dict[str, Any]:
        return {
            "endpoint": self._endpoint,
            "instrument_count": len(self._instruments),
            "last_flush_ok": self._last_flush_ok,
        }


class MetricsExportLoop:
    def __init__(
        self,
        clients: list[BigIPClientEntry],
        pusher: OTLPMetricsPusher,
        endpoint_resolver: EndpointResolver,
        *,
        tmctl_resolver: TmctlResolver | None = None,
        poll_interval_sec: float = 30.0,
    ) -> None:
        self._clients = clients
        self._pusher = pusher
        self._endpoint_resolver = endpoint_resolver
        self._tmctl_resolver = tmctl_resolver or (lambda _sid: [])
        self._poll_interval_sec = poll_interval_sec
        self._running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_run: float | None = None
        self._last_error: str | None = None
        self._last_point_count = 0
        self._last_errors_by_host: dict[str, list[str]] = {}
        self._last_endpoints_by_host: dict[str, list[str]] = {}
        self._last_tmctl_by_host: dict[str, list[str]] = {}

    @property
    def status(self) -> dict[str, Any]:
        endpoints_by_host = {
            host: self._endpoint_resolver(sid)
            for host, sid, _ in self._clients
        }
        tmctl_by_host = {
            host: self._tmctl_resolver(sid)
            for host, sid, _ in self._clients
        }
        return {
            "running": self._running,
            "endpoint_count": sum(len(eps) for eps in endpoints_by_host.values()),
            "endpoints_by_host": {
                host: len(eps) for host, eps in endpoints_by_host.items()
            },
            "metric_endpoints_by_host": endpoints_by_host,
            "tmctl_table_count": sum(len(t) for t in tmctl_by_host.values()),
            "tmctl_tables_by_host": tmctl_by_host,
            "bigip_count": len(self._clients),
            "bigip_hosts": [h for h, _, _ in self._clients],
            "last_run": self._last_run,
            "last_error": self._last_error,
            "last_point_count": self._last_point_count,
            "last_errors_by_host": self._last_errors_by_host,
            "last_endpoints_by_host": self._last_endpoints_by_host,
            "last_tmctl_by_host": self._last_tmctl_by_host,
            "poll_interval_sec": self._poll_interval_sec,
            "otlp": self._pusher.status,
        }

    def run_once(self) -> dict[str, Any]:
        from .metrics_extractor import extract_metrics
        from .tmctl import extract_tmctl_metrics

        total = 0
        errors: list[str] = []
        errors_by_host: dict[str, list[str]] = {}
        endpoints_by_host: dict[str, list[str]] = {}
        tmctl_by_host: dict[str, list[str]] = {}

        for host, sid, client in self._clients:
            endpoints = self._endpoint_resolver(sid)
            tables = self._tmctl_resolver(sid)
            endpoints_by_host[host] = endpoints
            tmctl_by_host[host] = tables
            host_errors: list[str] = []
            for ep in endpoints:
                try:
                    payload = client.get(ep)
                    points = extract_metrics(ep, payload, bigip_host=host)
                    total += self._pusher.record_batch(points)
                except Exception as exc:  # noqa: BLE001
                    msg = f"{ep}: {exc}"
                    errors.append(f"[{host}] {msg}")
                    host_errors.append(msg)
            for table in tables:
                try:
                    csv_text = client.query_tmctl_table(table)
                    points = extract_tmctl_metrics(table, csv_text, bigip_host=host)
                    total += self._pusher.record_batch(points)
                except Exception as exc:  # noqa: BLE001
                    msg = f"tmctl/{table}: {exc}"
                    errors.append(f"[{host}] {msg}")
                    host_errors.append(msg)
            if host_errors:
                errors_by_host[host] = host_errors[:10]

        flush_ok = self._pusher.force_flush()
        if not flush_ok:
            errors.append("OTLP force_flush failed — collector may not have received metrics")
        self._last_run = time.time()
        self._last_point_count = total
        self._last_errors_by_host = errors_by_host
        self._last_endpoints_by_host = endpoints_by_host
        self._last_tmctl_by_host = tmctl_by_host
        self._last_error = "; ".join(errors[:8]) if errors else None
        return {
            "points": total,
            "errors": errors,
            "errors_by_host": errors_by_host,
            "endpoints_by_host": endpoints_by_host,
            "tmctl_tables_by_host": tmctl_by_host,
            "flush_ok": flush_ok,
        }

    def start_background(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()

        def _loop() -> None:
            while self._running:
                try:
                    self.run_once()
                except Exception as exc:  # noqa: BLE001
                    self._last_error = str(exc)
                if self._stop_event.wait(timeout=self._poll_interval_sec):
                    break

        self._thread = threading.Thread(target=_loop, name="bigip-export-loop", daemon=True)
        self._thread.start()

    def stop(self, *, join_timeout_sec: float = 10.0) -> None:
        self._running = False
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=join_timeout_sec)
