"""Tests for BIG-IP metrics extraction and device rollups."""

from __future__ import annotations

import unittest

from backend.metrics_extractor import extract_metrics


class MetricsRollupTests(unittest.TestCase):
    def test_cpu_device_avg_max_across_cores(self) -> None:
        payload = {
            "entries": {
                "https://localhost/mgmt/tm/sys/cpu/0": {
                    "nestedStats": {
                        "entries": {
                            "cpuinfo.0": {"user": {"value": 10}},
                            "cpuinfo.1": {"user": {"value": 30}},
                        }
                    }
                }
            }
        }
        points = extract_metrics("/mgmt/tm/sys/cpu", payload, bigip_host="10.0.0.1")
        names = {p["name"] for p in points}
        self.assertIn("bigip_tm_sys_cpu_user", names)
        self.assertIn("bigip_tm_sys_cpu_user_device_avg", names)
        self.assertIn("bigip_tm_sys_cpu_user_device_max", names)

        avg = next(p for p in points if p["name"] == "bigip_tm_sys_cpu_user_device_avg")
        max_pt = next(p for p in points if p["name"] == "bigip_tm_sys_cpu_user_device_max")
        self.assertEqual(avg["value"], 20.0)
        self.assertEqual(max_pt["value"], 30.0)
        self.assertEqual(avg["attributes"], {"bigip.host": "10.0.0.1"})
        self.assertNotIn("bigip.object", avg["attributes"])

        per_core = [
            p for p in points
            if p["name"] == "bigip_tm_sys_cpu_user" and "bigip.object" in p["attributes"]
        ]
        self.assertEqual(len(per_core), 2)

    def test_memory_host_and_tmm_rollups(self) -> None:
        payload = {
            "entries": {
                "memory_host.0": {"memoryTotal": {"value": 1000}},
                "memory_tmm.0_0": {"memoryTotal": {"value": 400}},
                "memory_tmm.1_0": {"memoryTotal": {"value": 600}},
            }
        }
        points = extract_metrics("/mgmt/tm/sys/memory", payload, bigip_host="10.0.0.2")
        names = {p["name"] for p in points}
        self.assertIn("bigip_tm_sys_memory_memorytotal_host_avg", names)
        self.assertIn("bigip_tm_sys_memory_memorytotal_host_max", names)
        self.assertIn("bigip_tm_sys_memory_memorytotal_tmm_avg", names)
        self.assertIn("bigip_tm_sys_memory_memorytotal_tmm_max", names)

        host_avg = next(
            p for p in points if p["name"] == "bigip_tm_sys_memory_memorytotal_host_avg"
        )
        tmm_max = next(
            p for p in points if p["name"] == "bigip_tm_sys_memory_memorytotal_tmm_max"
        )
        self.assertEqual(host_avg["value"], 1000.0)
        self.assertEqual(tmm_max["value"], 600.0)


if __name__ == "__main__":
    unittest.main()
