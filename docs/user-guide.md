# User guide

This document is the detailed companion to the [User guide section in README](../README.md). It assumes the stack is already installed ([Ubuntu](../README.md#install-on-ubuntu-linux-without-kubernetes) or [Kubernetes](kubernetes.md)).

## Prerequisites

- UI reachable at `http://<HOST-IP>:8001`
- OpenTelemetry Collector and Prometheus running (Docker Compose on Ubuntu, or cluster workloads on Kubernetes)
- Network path from the **Python backend** to each BIG-IP management IP (HTTPS, typically port 443)

After upgrading the repo, rebuild the UI when not using Vite dev mode: `cd frontend && npm ci && npm run build`, then restart the API.

## UI layout

| Area | Purpose |
|------|---------|
| **Connected status bar** | Live count, chip per connected BIG-IP, export selection summary, **Refresh list** (also auto-refreshes every 45s) |
| **BIG-IP connections** | Manage sessions, check devices for export, connect form |
| **API endpoints** | iControl REST path catalog |
| **Collector exporters** | OpenTelemetry Collector Contrib sinks |
| **Export to collector** | OTLP push from the Python backend |
| **Prometheus validation** | Targets, queries, reload/restart |

## 1. Connect BIG-IP devices

### First device

1. Open the UI.
2. In **BIG-IP connections**, enter **Management host**, **Username**, and **Password**.
3. Optionally set a **Label** (shown in the device list).
4. Uncheck **Verify TLS** if the management certificate is self-signed.
5. Click **Connect** (the button stays disabled until host, username, and password are all provided).

### Additional devices

1. Enter the next management host (and credentials if different).
2. Click **Add BIG-IP** (same required fields).
3. Repeat for each device you want to monitor.

The **Connected status bar** at the top always shows how many BIG-IPs are connected and lists each one. The connections card title includes the count, e.g. **BIG-IP connections (2 connected)**.

### Device list

| Control | Action |
|---------|--------|
| Checkbox | Include device in export |
| **Remove** | Disconnect (`DELETE /api/session/{session_id}`) |
| Warning text | Token extension failed; session may expire sooner |

Reconnecting the same management IP **replaces** the existing session for that host.

### Security note

Credentials are held in the API process memory for active sessions. They are not stored in Kubernetes Secrets or on disk by default. Restarting the backend pod/process clears all sessions.

## 2. Select API endpoints

The catalog is loaded from `data/bigip_apis.csv`.

- Enable **Metrics / stats endpoints only** for recommended paths.
- Use **Module filter** to narrow by LTM, DNS, etc.
- Use **Select all visible** or per-row checkboxes.

Stats endpoints (paths containing `/stats`) typically expose counters and gauges suitable for export.

## 3. Collector exporters (optional)

The stack always exposes a Prometheus exporter on port **8889** for validation. Additional exporters use [OpenTelemetry Collector Contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter) components (Datadog, Kafka, Prometheus remote write, Splunk HEC, cloud backends, and more).

1. In **OpenTelemetry Collector exporters**, choose a type (grouped by category) and fill in its fields.
2. For exporters not in the list, use **Contrib exporter (custom YAML)**: pick the collector component name (e.g. `loadbalancing`, `zipkin`) and paste the YAML settings from upstream docs.
3. Click **Apply collector config**.
4. Restart the collector:

```bash
# Ubuntu
docker compose restart otel-collector

# Kubernetes (backend port-forward on :8001)
./scripts/k8s-apply-collector-config.sh
```

Generated file: `otel-collector/generated-config.yaml`

## 4. Export metrics

### UI settings

| Field | Ubuntu | Kubernetes |
|-------|--------|------------|
| OTLP HTTP endpoint | `http://127.0.0.1:4318` | `http://otel-collector.bigip-metrics.svc.cluster.local:4318` |
| Poll interval | Seconds between full cycles (default 30) | Same |

Ensure at least one device is **checked** in the connections list, then click **Start export**.

### What happens

For each poll cycle, for each selected device and endpoint:

1. `GET` the iControl REST path on that BIG-IP.
2. Parse nested `entries` / `nestedStats` into metric points.
3. Attach attributes: `bigip.host`, `bigip.management_ip`, `bigip.endpoint`.
4. Push batches via OTLP HTTP to the collector.
5. Collector forwards to the Prometheus exporter and any configured exporters.

### Status fields

Use **Refresh status** or `GET /api/export/status`:

| Field | Meaning |
|-------|---------|
| `running` | Background poll loop active |
| `bigip_count` / `bigip_hosts` | Devices in this export |
| `last_point_count` | Points recorded in last cycle |
| `last_errors_by_host` | Per-device API errors (truncated) |
| `connected_devices` | Current UI sessions |

### REST API example

```bash
curl -s http://127.0.0.1:8001/api/bigips
curl -s -X POST http://127.0.0.1:8001/api/export/start \
  -H 'Content-Type: application/json' \
  -d '{
    "session_ids": [],
    "endpoints": ["/mgmt/tm/ltm/virtual/stats"],
    "poll_interval_sec": 30,
    "otlp_endpoint": "http://127.0.0.1:4318"
  }'
```

Empty `session_ids` exports **all** connected devices. To target specific devices, pass their `session_id` values from `/api/bigips`.

## 5. Validate in Prometheus

| Surface | URL |
|---------|-----|
| Prometheus UI | `http://<HOST-IP>:9090` |
| Collector metrics | `http://<HOST-IP>:8889/metrics` |

### Checks

1. **Status → Targets** — `otel-collector` job should be **UP**.
2. Run a broad query: `bigip_tm_ltm_virtual_stats` or search `bigip_`.
3. With multiple devices, use metric families plus labels (not one series per URL blob):

   | Label | Meaning |
   |-------|---------|
   | `bigip_host` | BIG-IP management address |
   | `bigip_stat` | Stat field name (`memoryfree`, `clientside.bitsIn`, …) |
   | `bigip_object` | Short stats object slot (e.g. `memory_host_0`) |

   ```promql
   bigip_tm_sys_memory{bigip_host="10.0.0.50", bigip_stat="memoryused"}
   sum by (bigip_host, bigip_stat) (bigip_tm_sys_memory)
   ```

4. Use **Reload Prometheus (wipe data)** in the UI to clear TSDB and reload scrape config. By default this recreates the Prometheus container (Docker) or pod (Kubernetes), or deletes all series via the admin API when orchestration tools are unavailable. Set backend env `PROMETHEUS_RELOAD_WIPE_TSDB=false` to reload config only.
5. Use **Restart Prometheus** for a recreate without the separate `/-/reload` step.

## Multi-BIG-IP reference

| Topic | Detail |
|-------|--------|
| API list devices | `GET /api/bigips` or `GET /api/devices` |
| Connect | `POST /api/connect` with optional `label` |
| Metric collision | One metric name per REST stats endpoint; separated by `bigip_host`, `bigip_stat`, `bigip_object` labels |
| Export scope (UI) | Checked devices only |
| Export scope (API) | `session_ids` array; empty = all connected |

## Troubleshooting

| Symptom | Checks |
|---------|--------|
| Connect fails | `curl -sk https://<IP>/mgmt/shared/ident` from API host/pod |
| 401 | Credentials, account lockout, REST role |
| Token warning | Reconnect; or accept shorter session lifetime |
| No metrics | Export running? OTLP URL correct for environment? Collector logs |
| One device only | Multiple devices checked? PromQL `bigip_host` label |
| UI 404 | `frontend/dist` built; API restarted |

See also [Ubuntu troubleshooting](../README.md#ubuntu-troubleshooting) and [Kubernetes troubleshooting](kubernetes.md#troubleshooting).
