# Kubernetes installation guide

This document is the detailed companion to the [Kubernetes section in README](../README.md). For day-to-day UI workflow (multi-BIG-IP, export, Prometheus), see the **[User guide](user-guide.md)**.

It describes how to run the **entire** BIG-IP Telemetry Exporter stack on Kubernetes:

- **bigip-telemetry-backend** — Python API + React UI (single container image)
- **otel-collector** — OpenTelemetry Collector Contrib
- **prometheus** — scrapes the collector’s Prometheus exporter for validation

## Prerequisites

| Requirement | Notes |
|-------------|--------|
| Kubernetes 1.25+ | Any common distribution (EKS, GKE, AKS, OpenShift, k3s, etc.) |
| `kubectl` | Configured for your cluster |
| Container runtime | To build/push the backend image (`docker` or `podman`) |
| Network path to BIG-IP | Pods must reach the BIG-IP management IP (typically HTTPS :443) |
| Optional Ingress | Only if you use the `base` or `example` overlay with Ingress enabled |

## Manifest layout

```
k8s/
├── base/                          # Full stack (includes sample Ingress)
│   ├── namespace.yaml
│   ├── configmap-otel-collector.yaml
│   ├── configmap-prometheus.yaml
│   ├── rbac-prometheus.yaml
│   ├── deployment-*.yaml
│   ├── service-*.yaml
│   ├── ingress.yaml
│   └── kustomization.yaml
└── overlays/
    ├── minimal/                   # Same stack, no Ingress
    └── example/                   # Sample registry + hostnames
```

## Install steps

### 1. Build and publish the backend image

The backend image bundles the React UI (`frontend/dist`) and Python API.

```bash
chmod +x scripts/k8s-build-image.sh
./scripts/k8s-build-image.sh

# Tag and push to your registry, for example:
docker tag bigip-telemetry-exporter:latest ghcr.io/<org>/bigip-telemetry-exporter:1.0.0
docker push ghcr.io/<org>/bigip-telemetry-exporter:1.0.0
```

### 2. Choose an overlay

| Overlay | Use when |
|---------|----------|
| **`local`** | You built the image locally and loaded it into kind/minikube/k3d (or Docker Desktop) |
| `minimal` | Image is in a **registry**; set `IMAGE=...` when running `k8s-deploy.sh` |
| `example` | Registry + Ingress hostnames (edit `kustomization.yaml`) |
| `base` | Raw manifests; same image requirements as `minimal` |

> **Important:** `bigip-telemetry-exporter:latest` is **not** on Docker Hub. An unqualified name resolves to `docker.io/library/bigip-telemetry-exporter`, which causes `ErrImagePull` / `authorization failed`.

### 3. Deploy

**Local image (recommended for dev):**

```bash
chmod +x scripts/k8s-build-image.sh scripts/k8s-load-image.sh scripts/k8s-deploy.sh
./scripts/k8s-build-image.sh
./scripts/k8s-load-image.sh
./scripts/k8s-deploy.sh local
```

**Registry:**

```bash
export IMAGE=ghcr.io/<org>/bigip-telemetry-exporter:1.0.0
docker tag bigip-telemetry-exporter:latest "${IMAGE}"
docker push "${IMAGE}"
IMAGE="${IMAGE}" ./scripts/k8s-deploy.sh minimal
```

### 4. Access the UI and Prometheus

**Port-forward (minimal overlay):**

Use `--address 0.0.0.0` so the UI is reachable via your machine’s IP, not only `127.0.0.1`:

```bash
export HOST_IP="$(./scripts/host-ip.sh)"   # LAN IP shown to clients

kubectl -n bigip-telemetry port-forward --address 0.0.0.0 svc/bigip-telemetry-backend 8001:8000
kubectl -n bigip-telemetry port-forward --address 0.0.0.0 svc/prometheus 9090:9090
```

- UI + API: `http://<HOST-IP>:8001` (local port **8001** → service port 8000)  
- Prometheus: `http://<HOST-IP>:9090`  

Prometheus links in the UI use the hostname from your browser (e.g. `192.168.1.10:8001`), or set `ACCESS_HOST` on the backend Deployment.

**Ingress:** Update hosts in `k8s/base/ingress.yaml` or the `example` overlay patch, set `ingressClassName` for your controller, then apply an overlay that includes Ingress.

### 5. Configure collector exporters from the UI

1. Open the UI → **OpenTelemetry Collector exporters** → **Apply collector config**.
2. Sync YAML into the cluster ConfigMap and restart the collector:

```bash
# With backend port-forward running on :8001
chmod +x scripts/k8s-apply-collector-config.sh
./scripts/k8s-apply-collector-config.sh
```

Or manually:

```bash
kubectl -n bigip-telemetry create configmap otel-collector-config \
  --from-file=config.yaml=./otel-collector/generated-config.yaml \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n bigip-telemetry rollout restart deployment/otel-collector
```

### 6. Connect to BIG-IP and start export

See the **[User guide](user-guide.md)** for the full workflow. Summary:

1. Open the UI (port-forward `8001:8000`).
2. **Connect** the first BIG-IP, then **Add BIG-IP** for additional devices.
3. Check which devices to include in export.
4. Select API endpoints and **Start export**.

Credentials are **not** stored in Kubernetes Secrets by default—they are entered in the UI per session (same as local/docker). Ensure:

- Every management IP is reachable from pods.
- TLS verification matches your BIG-IP cert (checkbox in UI).

The backend uses `OTLP_HTTP_ENDPOINT=http://otel-collector.bigip-telemetry.svc.cluster.local:4318` and listens on container port **8000** (`PORT=8000` in the Deployment).

### 7. Validate metrics in Prometheus

In Prometheus, run a query such as:

```promql
bigip_tm_ltm_virtual_stats
```

With multiple BIG-IPs:

```promql
bigip_tm_ltm_virtual_stats{bigip_host="10.0.0.50"}
```

Confirm the `otel-collector` scrape target is **UP** (Status → Targets). Use **Reload Prometheus** or **Restart Prometheus** in the UI when needed.

## Environment variables (backend)

| Variable | Default (K8s manifest) | Purpose |
|----------|------------------------|---------|
| `PORT` | `8000` | HTTP listen port inside the container (Service targets 8000) |
| `OTLP_HTTP_ENDPOINT` | `http://otel-collector.bigip-telemetry.svc.cluster.local:4318` | Backend → collector (in-cluster) |
| `ACCESS_HOST` | *(unset)* | Force browser link hostname; default = HTTP `Host` header |
| `PROMETHEUS_BROWSER_PORT` | `9090` | Prometheus UI port on the host running port-forward |
| `COLLECTOR_METRICS_BROWSER_PORT` | `8889` | Collector `/metrics` port on the host |
| `PROMETHEUS_UI_URL` | *(optional)* | Override auto-detected Prometheus URL |
| `COLLECTOR_METRICS_URL` | *(optional)* | Override auto-detected collector metrics URL |
| `COLLECTOR_CONFIG_PATH` | `/tmp/collector-config.yaml` | Where API writes generated YAML |
| `COLLECTOR_RESTART_HINT` | `kubectl ... rollout restart ...` | Shown after Apply in UI |

## BIG-IP network connectivity

The exporter pod must reach BIG-IP management interfaces. Common patterns:

- BIG-IP on a routable network from worker nodes.
- Cloud hybrid / VPN / transit gateway.
- `hostNetwork: true` on the backend Deployment (only if required by your CNI/security policy)—add via a custom overlay patch.

If connection fails from the UI but works locally on your laptop, debug with:

```bash
kubectl -n bigip-telemetry exec -it deploy/bigip-telemetry-backend -- \
  python -c "import urllib.request; urllib.request.urlopen('https://<BIG-IP-MGMT-IP>/mgmt/shared/ident', context=__import__('ssl')._create_unverified_context())"
```

## Upgrades

```bash
./scripts/k8s-build-image.sh
docker push <your-image>:<tag>
# Update image tag in overlay kustomization.yaml
kubectl apply -k k8s/overlays/minimal
kubectl -n bigip-telemetry rollout status deployment/bigip-telemetry-backend
```

## Uninstall

Use [`scripts/k8s-uninstall.sh`](../scripts/k8s-uninstall.sh) with the **same overlay** you used to deploy:

```bash
chmod +x scripts/k8s-uninstall.sh
./scripts/k8s-uninstall.sh local
```

| Option | Effect |
|--------|--------|
| `-y` / `--yes` | Skip confirmation |
| `--keep-namespace` | Delete Deployments/Services/ConfigMaps/RBAC; leave `bigip-telemetry` namespace |

Examples:

```bash
./scripts/k8s-uninstall.sh minimal -y
./scripts/k8s-uninstall.sh example --keep-namespace
```

Manual equivalent:

```bash
kubectl delete -k k8s/overlays/local --wait --timeout=180s
```

The namespace `bigip-telemetry` and all workloads (otel-collector, prometheus, backend) are removed. Port-forward processes on your workstation are not stopped automatically — press Ctrl+C in those terminals.

Optional cleanup on your build host:

```bash
docker rmi bigip-telemetry-exporter:latest
```

## Troubleshooting

| Symptom | Check |
|---------|--------|
| `ErrImagePull` / `authorization failed` for `bigip-telemetry-exporter` | Image is not on Docker Hub. Use `./scripts/k8s-deploy.sh local` after build+load, or `IMAGE=<registry>/... ./scripts/k8s-deploy.sh minimal` after push |
| Backend `ImagePullBackOff` | Same as above; `kubectl describe pod` → Events |
| No metrics in Prometheus | `kubectl logs deploy/otel-collector`; export started in UI? |
| OTLP errors in backend logs | Service `otel-collector` endpoints; port 4318 |
| BIG-IP login fails | Network/firewall; TLS verify setting |
| Ingress 404 | Host/DNS; `ingressClassName`; backend service port 8000 |
