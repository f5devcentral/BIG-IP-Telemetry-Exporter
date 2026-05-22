#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${IMAGE:-bigip-metrics-exporter:latest}"
echo "Building ${IMAGE} from ${ROOT}"
docker build -t "${IMAGE}" "${ROOT}"
echo "Done. Push to your registry, then set the image in k8s/overlays/*/kustomization.yaml"
