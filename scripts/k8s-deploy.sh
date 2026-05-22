#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OVERLAY="${1:-minimal}"
KUSTOMIZE_PATH="${ROOT}/k8s/overlays/${OVERLAY}"

if [[ ! -d "${KUSTOMIZE_PATH}" ]]; then
  echo "Unknown overlay: ${OVERLAY}" >&2
  echo "Available: minimal, example" >&2
  exit 1
fi

echo "Applying kustomize overlay: ${OVERLAY}"
kubectl apply -k "${KUSTOMIZE_PATH}"
echo ""
echo "Waiting for rollouts..."
kubectl -n bigip-metrics rollout status deployment/otel-collector --timeout=120s
kubectl -n bigip-metrics rollout status deployment/prometheus --timeout=120s
kubectl -n bigip-metrics rollout status deployment/bigip-metrics-backend --timeout=120s
echo ""
echo "Pods:"
kubectl -n bigip-metrics get pods
echo ""
echo "Access UI (port-forward):"
echo "  kubectl -n bigip-metrics port-forward svc/bigip-metrics-backend 8000:8000"
echo "Prometheus:"
echo "  kubectl -n bigip-metrics port-forward svc/prometheus 9090:9090"
