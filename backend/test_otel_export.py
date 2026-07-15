"""Tests for OTLP metrics pusher MeterProvider lifecycle."""

from __future__ import annotations

import unittest
from unittest import mock

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource

from backend.otel_export import OTLPMetricsPusher


def _points(name: str, value: float, **attrs: str) -> list[dict]:
    return [{"name": name, "value": value, "attributes": attrs}]


class OtlpPusherTests(unittest.TestCase):
    def test_stop_start_records_without_global_meter_provider(self) -> None:
        """
        OpenTelemetry forbids replacing the global MeterProvider. Using
        provider.get_meter() allows stop/start (and nuke) to keep exporting.
        """
        readers: list[InMemoryMetricReader] = []

        def _memory_pusher(endpoint: str, *, interval_ms: int = 5000) -> OTLPMetricsPusher:
            pusher = OTLPMetricsPusher.__new__(OTLPMetricsPusher)
            mem = InMemoryMetricReader()
            provider = MeterProvider(
                resource=Resource.create({"service.name": "test"}),
                metric_readers=[mem],
            )
            pusher._endpoint = endpoint.rstrip("/").removesuffix("/v1/metrics")
            pusher._provider = provider
            pusher._meter = provider.get_meter("bigip.metrics")
            pusher._instruments = {}
            pusher._last_flush_ok = None
            readers.append(mem)
            return pusher

        first = _memory_pusher("http://127.0.0.1:4318")
        first.record_batch(_points("tm_sys_cpu_one_min_avg", 12.5, **{"bigip.host": "a"}))
        self.assertTrue(first.force_flush())
        names1 = {
            m.name
            for rm in readers[0].get_metrics_data().resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
        }
        self.assertIn("tm_sys_cpu_one_min_avg", names1)
        first.shutdown()

        second = _memory_pusher("http://127.0.0.1:4318")
        second.record_batch(_points("tm_sys_memory_used", 99.0, **{"bigip.host": "b"}))
        self.assertTrue(second.force_flush())
        names2 = {
            m.name
            for rm in readers[1].get_metrics_data().resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
        }
        self.assertIn("tm_sys_memory_used", names2)
        second.shutdown()

    def test_constructor_does_not_set_global_meter_provider(self) -> None:
        with mock.patch("opentelemetry.metrics.set_meter_provider") as set_mp:
            with mock.patch(
                "backend.otel_export.OTLPMetricExporter",
            ) as exp_cls:
                exporter = mock.Mock()
                exporter._preferred_temporality = None
                exporter._preferred_aggregation = None
                exporter.export.return_value = mock.Mock()
                exporter.force_flush.return_value = True
                exporter.shutdown.return_value = True
                exp_cls.return_value = exporter
                pusher = OTLPMetricsPusher("http://127.0.0.1:4318")
                pusher.shutdown()
        set_mp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
