#!/usr/bin/env bash
# Load bigip-telemetry-exporter:latest into a local Kubernetes cluster (kind, minikube, k3d).
set -euo pipefail
IMAGE="${IMAGE:-bigip-telemetry-exporter:latest}"

if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  echo "Image ${IMAGE} not found locally. Run ./scripts/k8s-build-image.sh first." >&2
  exit 1
fi

if command -v kind >/dev/null 2>&1 && kind get clusters 2>/dev/null | grep -q .; then
  echo "Loading ${IMAGE} into kind..."
  kind load docker-image "${IMAGE}" --name "$(kind get clusters | head -1)"
  echo "Done (kind)."
  exit 0
fi

if command -v minikube >/dev/null 2>&1 && minikube status >/dev/null 2>&1; then
  echo "Loading ${IMAGE} into minikube..."
  minikube image load "${IMAGE}"
  echo "Done (minikube)."
  exit 0
fi

if command -v k3d >/dev/null 2>&1 && k3d cluster list -o json 2>/dev/null | grep -q '"name"'; then
  CLUSTER="$(k3d cluster list | awk 'NR==2 {print $1}')"
  echo "Loading ${IMAGE} into k3d cluster ${CLUSTER}..."
  k3d image import "${IMAGE}" -c "${CLUSTER}"
  echo "Done (k3d)."
  exit 0
fi

echo "No kind/minikube/k3d cluster detected." >&2
echo "Docker Desktop / k3s often use the host Docker socket — IfNotPresent may work after ./scripts/k8s-build-image.sh without loading." >&2
echo "Otherwise push to a registry and deploy with: IMAGE=<registry>/bigip-telemetry-exporter:<tag> ./scripts/k8s-deploy.sh minimal" >&2
exit 1
