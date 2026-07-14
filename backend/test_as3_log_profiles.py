"""Tests for AS3 remote-log pool member construction."""

from __future__ import annotations

import unittest

from backend.as3_log_profiles import _pool_member


class PoolMemberTests(unittest.TestCase):
    def test_ip_member_uses_static_server_addresses(self) -> None:
        member = _pool_member("10.1.1.50", 5141)
        self.assertEqual(member["addressDiscovery"], "static")
        self.assertEqual(member["serverAddresses"], ["10.1.1.50"])
        self.assertEqual(member["servicePort"], 5141)
        self.assertTrue(member["shareNodes"])
        self.assertNotIn("hostname", member)

    def test_hostname_member_uses_fqdn_discovery(self) -> None:
        member = _pool_member("logs.example.com", 5140)
        self.assertEqual(member["addressDiscovery"], "fqdn")
        self.assertEqual(member["hostname"], "logs.example.com")
        self.assertEqual(member["servicePort"], 5140)
        self.assertTrue(member["autoPopulate"])
        self.assertTrue(member["shareNodes"])
        self.assertNotIn("serverAddresses", member)


if __name__ == "__main__":
    unittest.main()
