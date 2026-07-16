"""Tests for tmctl table listing and CSV metric extraction."""

from __future__ import annotations

import unittest

from backend.bigip_client import BigIPError
from backend.tmctl import (
    build_tmctl_list_command,
    build_tmctl_query_command,
    extract_tmctl_metrics,
    parse_tmctl_table_list,
    validate_tmctl_table_name,
)


class TmctlParseTests(unittest.TestCase):
    def test_validate_rejects_shell_injection(self) -> None:
        with self.assertRaises(BigIPError):
            validate_tmctl_table_name("memory_usage_stat; rm -rf /")
        with self.assertRaises(BigIPError):
            validate_tmctl_table_name("../etc/passwd")
        self.assertEqual(validate_tmctl_table_name("memory_usage_stat"), "memory_usage_stat")

    def test_parse_tmctl_a_list(self) -> None:
        text = """
memory_usage_stat
page_stats
umem_cache_0

.ignored
---
ssl_sid_cache
"""
        self.assertEqual(
            parse_tmctl_table_list(text),
            ["memory_usage_stat", "page_stats", "umem_cache_0", "ssl_sid_cache"],
        )

    def test_build_commands_are_quoted_safely(self) -> None:
        self.assertEqual(build_tmctl_list_command(), "-c 'tmctl -a'")
        self.assertEqual(
            build_tmctl_query_command("page_stats"),
            "-c 'tmctl -c page_stats'",
        )
        with self.assertRaises(BigIPError):
            build_tmctl_query_command("x'; y")

    def test_extract_csv_metrics(self) -> None:
        csv_text = (
            "name,allocated,max_allocated,size\n"
            "persist,0,0,160\n"
            "ssl,24340896,24340896,1\n"
        )
        points = extract_tmctl_metrics(
            "memory_usage_stat",
            csv_text,
            bigip_host="10.0.0.1",
        )
        names = {p["name"] for p in points}
        self.assertIn("bigip_tmctl_memory_usage_stat_allocated", names)
        ssl_alloc = next(
            p
            for p in points
            if p["name"] == "bigip_tmctl_memory_usage_stat_allocated"
            and p["attributes"].get("tmctl.name") == "ssl"
        )
        self.assertEqual(ssl_alloc["value"], 24340896.0)
        self.assertEqual(ssl_alloc["attributes"]["bigip.host"], "10.0.0.1")
        self.assertEqual(ssl_alloc["attributes"]["tmctl.table"], "memory_usage_stat")

    def test_extract_page_stats_identity(self) -> None:
        csv_text = "slot,tmid,pages_used,pages_avail\n0,0,123882,218624\n0,1,68244,219648\n"
        points = extract_tmctl_metrics("page_stats", csv_text, bigip_host="bigip1")
        used = [
            p
            for p in points
            if p["name"] == "bigip_tmctl_page_stats_pages_used"
        ]
        self.assertEqual(len(used), 2)
        self.assertEqual(used[0]["attributes"]["tmctl.slot"], "0")
        self.assertEqual(used[0]["attributes"]["tmctl.tmid"], "0")


if __name__ == "__main__":
    unittest.main()
