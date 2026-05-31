#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${IMAGE:-bigip-telemetry-exporter:latest}"
echo "Building ${IMAGE} from ${ROOT}"
docker build -t "${IMAGE}" "${ROOT}"
echo "Done."
echo "  Local cluster:  ./scripts/k8s-load-image.sh && ./scripts/k8s-deploy.sh local"
echo "  Registry:       export IMAGE=ghcr.io/<you>/bigip-telemetry-exporter:1.0.0"
echo "                  docker tag bigip-telemetry-exporter:latest \"\${IMAGE}\" && docker push \"\${IMAGE}\""
echo "                  IMAGE=\"\${IMAGE}\" ./scripts/k8s-deploy.sh minimal"
