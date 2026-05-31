#!/usr/bin/env bash
# Remove the BIG-IP Telemetry Exporter stack from Kubernetes.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OVERLAY="local"
CONFIRM=""
DELETE_NAMESPACE=1

usage() {
  cat <<'EOF'
Usage: ./scripts/k8s-uninstall.sh [overlay] [options]

Overlays (use the same overlay you deployed with k8s-deploy.sh):
  local     default
  minimal
  example

Options:
  -y, --yes              Skip confirmation prompt
  -n, --keep-namespace   Remove workloads but keep namespace bigip-telemetry
  -h, --help             Show this help

Environment:
  CONFIRM=1              Same as --yes

Examples:
  ./scripts/k8s-uninstall.sh local
  ./scripts/k8s-uninstall.sh minimal -y
  ./scripts/k8s-uninstall.sh -y example
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes)
      CONFIRM=1
      shift
      ;;
    -n|--keep-namespace)
      DELETE_NAMESPACE=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    local|minimal|example)
      OVERLAY="$1"
      shift
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      OVERLAY="$1"
      shift
      ;;
  esac
done

KUSTOMIZE_PATH="${ROOT}/k8s/overlays/${OVERLAY}"

if [[ ! -d "${KUSTOMIZE_PATH}" ]]; then
  echo "Unknown overlay: ${OVERLAY}" >&2
  echo "Available: local, minimal, example" >&2
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl not found in PATH" >&2
  exit 1
fi

if ! kubectl get namespace bigip-telemetry >/dev/null 2>&1; then
  echo "Namespace bigip-telemetry does not exist — nothing to uninstall."
  exit 0
fi

echo "Remove BIG-IP Telemetry Exporter (overlay: ${OVERLAY})"
echo "  Path: ${KUSTOMIZE_PATH}"
if [[ "${DELETE_NAMESPACE}" == "1" ]]; then
  echo "  Will delete all resources and namespace bigip-telemetry."
else
  echo "  Will delete workloads; namespace bigip-telemetry will remain."
fi

if [[ -z "${CONFIRM}" ]]; then
  read -r -p "Continue? [y/N] " reply
  case "${reply}" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 0 ;;
  esac
fi

echo ""
if [[ "${DELETE_NAMESPACE}" == "1" ]]; then
  echo "Running: kubectl delete -k ${KUSTOMIZE_PATH} --wait"
  kubectl delete -k "${KUSTOMIZE_PATH}" --ignore-not-found --wait=true --timeout=180s
else
  echo "Deleting namespaced resources in bigip-telemetry..."
  kubectl -n bigip-telemetry delete deployment,service,configmap,ingress --all \
    --ignore-not-found --wait=true --timeout=180s
  kubectl -n bigip-telemetry delete role,rolebinding,serviceaccount --all \
    --ignore-not-found --wait=true --timeout=60s
fi

echo ""
if kubectl get namespace bigip-telemetry >/dev/null 2>&1; then
  echo "Remaining resources in bigip-telemetry:"
  kubectl -n bigip-telemetry get all 2>/dev/null || true
else
  echo "Namespace bigip-telemetry removed."
fi

echo ""
echo "Uninstall complete."
echo "Tip: stop any active port-forward sessions (Ctrl+C in those terminals)."
echo "Local image bigip-telemetry-exporter:latest was not removed."
echo "  docker rmi bigip-telemetry-exporter:latest   # optional"
