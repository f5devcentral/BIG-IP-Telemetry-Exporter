"""Tests for Kubernetes collector restart helpers."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from backend import collector_ops


class K8sHelperTests(unittest.TestCase):
    def test_deployment_name_strips_resource_prefix(self) -> None:
        with mock.patch.object(collector_ops, "COLLECTOR_K8S_DEPLOYMENT", "deployment/otel-collector"):
            self.assertEqual(collector_ops._k8s_deployment_name(), "otel-collector")

    def test_namespace_prefers_env(self) -> None:
        with mock.patch.dict(os.environ, {"COLLECTOR_K8S_NAMESPACE": "custom-ns"}, clear=False):
            self.assertEqual(collector_ops._k8s_namespace(), "custom-ns")

    def test_in_cluster_detected_from_service_host(self) -> None:
        with mock.patch.dict(os.environ, {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, clear=False):
            self.assertTrue(collector_ops._in_cluster())
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("KUBERNETES_SERVICE_HOST", None)
            self.assertFalse(collector_ops._in_cluster())


if __name__ == "__main__":
    unittest.main()
