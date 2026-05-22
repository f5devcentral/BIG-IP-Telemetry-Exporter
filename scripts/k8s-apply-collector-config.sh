#!/usr/bin/env bash
# Sync OpenTelemetry Collector config from the running API into the cluster ConfigMap.
set -euo pipefail
NAMESPACE="${NAMESPACE:-bigip-metrics}"
API_URL="${API_URL:-http://127.0.0.1:8000}"
TMP="$(mktemp -t collector-config.XXXXXX.yaml)"

cleanup() { rm -f "${TMP}"; }
trap cleanup EXIT

echo "Fetching collector YAML from ${API_URL}/api/collector/config ..."
curl -fsS "${API_URL}/api/collector/config" | python3 -c "
import json, sys
print(json.load(sys.stdin)['yaml'])
" > "${TMP}"

echo "Updating ConfigMap otel-collector-config in namespace ${NAMESPACE} ..."
kubectl -n "${NAMESPACE}" create configmap otel-collector-config \
  --from-file=config.yaml="${TMP}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Restarting otel-collector ..."
kubectl -n "${NAMESPACE}" rollout restart deployment/otel-collector
kubectl -n "${NAMESPACE}" rollout status deployment/otel-collector --timeout=120s
echo "Collector config applied."
