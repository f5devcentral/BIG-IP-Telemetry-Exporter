"""Tests for tmctl table listing and CSV metric extraction."""

from __future__ import annotations

import unittest

from backend.bigip_client import BigIPError
from backend.tmctl import (
    TMCTL_STATS_TABLES,
    build_tmctl_query_command,
    extract_tmctl_metrics,
    list_tmctl_stats_tables,
    parse_tmctl_table_list,
    validate_tmctl_stats_table,
    validate_tmctl_table_name,
)


class TmctlParseTests(unittest.TestCase):
    def test_curated_catalog_has_11_tables(self) -> None:
        tables = list_tmctl_stats_tables()
        self.assertEqual(len(tables), 11)
        self.assertEqual(tables, list(TMCTL_STATS_TABLES))
        self.assertIn("proc_stat", tables)
        self.assertIn("tmm_stat", tables)

    def test_validate_stats_table_rejects_unknown(self) -> None:
        with self.assertRaises(BigIPError):
            validate_tmctl_stats_table("page_stats")
        self.assertEqual(validate_tmctl_stats_table("proc_stat"), "proc_stat")

    def test_validate_rejects_shell_injection(self) -> None:
        with self.assertRaises(BigIPError):
            validate_tmctl_table_name("memory_usage_stat; rm -rf /")
        with self.assertRaises(BigIPError):
            validate_tmctl_table_name("../etc/passwd")

    def test_parse_tmctl_a_list_legacy(self) -> None:
        text = """
memory_usage_stat
page_stats
umem_cache_0
"""
        self.assertEqual(
            parse_tmctl_table_list(text),
            ["memory_usage_stat", "page_stats", "umem_cache_0"],
        )

    def test_build_query_command_is_quoted_safely(self) -> None:
        self.assertEqual(
            build_tmctl_query_command("proc_stat"),
            "-c 'tmctl -c proc_stat'",
        )
        with self.assertRaises(BigIPError):
            build_tmctl_query_command("x'; y")

    def test_extract_csv_metrics(self) -> None:
        csv_text = "name,cpu_usage\nproc1,42\nproc2,17\n"
        points = extract_tmctl_metrics(
            "proc_stat",
            csv_text,
            bigip_host="10.0.0.1",
        )
        names = {p["name"] for p in points}
        self.assertIn("bigip_tmctl_proc_stat_cpu_usage", names)
        proc1 = next(
            p
            for p in points
            if p["name"] == "bigip_tmctl_proc_stat_cpu_usage"
            and p["attributes"].get("tmctl.name") == "proc1"
        )
        self.assertEqual(proc1["value"], 42.0)
        self.assertEqual(proc1["attributes"]["bigip.host"], "10.0.0.1")
        self.assertEqual(proc1["attributes"]["tmctl.table"], "proc_stat")

    def test_extract_proc_stat(self) -> None:
        csv_proc = "name,cur_conns,tot_conns\nvs1,10,100\n"
        points = extract_tmctl_metrics("proc_stat", csv_proc, bigip_host="bigip1")
        self.assertTrue(any(p["name"].startswith("bigip_tmctl_proc_stat_") for p in points))
        conns = next(p for p in points if p["name"] == "bigip_tmctl_proc_stat_cur_conns")
        self.assertEqual(conns["value"], 10.0)
        self.assertEqual(conns["attributes"]["tmctl.name"], "vs1")


if __name__ == "__main__":
    unittest.main()
