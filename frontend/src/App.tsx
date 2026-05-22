import { useCallback, useEffect, useMemo, useState } from "react";
import f5LogoUrl from "./assets/F5-logo-F5-rgb.svg";

const THEME_STORAGE_KEY = "bigip-metrics-ui-theme";
type ThemeMode = "light" | "dark" | "system";

type ApiRow = {
  category: string;
  endpoint: string;
  description: string;
  status: string;
  module: string;
  notes: string;
  collect_metrics: string;
};

type ExporterCatalogItem = { type: string; label: string; description: string };

type ExporterConfig = {
  type: string;
  enabled: boolean;
  params: Record<string, string | number | boolean>;
};

async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(path, init);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    throw new Error(`Network error calling ${path}: ${message}`);
  }
}

async function readJson<T>(r: Response): Promise<T> {
  const text = await r.text();
  if (!r.ok) {
    let detail = text;
    try {
      const j = JSON.parse(text) as { detail?: string | Array<{ msg?: string }> };
      if (typeof j.detail === "string") {
        detail = j.detail;
      } else if (Array.isArray(j.detail)) {
        detail = j.detail.map((d) => d.msg ?? JSON.stringify(d)).join("; ");
      }
    } catch {
      /* ignore */
    }
    const snip = detail.replace(/\s+/g, " ").trim().slice(0, 500);
    throw new Error(
      snip
        ? `${r.status} ${r.statusText}: ${snip}`
        : `${r.status} ${r.statusText}`,
    );
  }
  return text ? (JSON.parse(text) as T) : ({} as T);
}

const DEFAULT_EXPORTERS: ExporterConfig[] = [
  { type: "prometheus", enabled: true, params: { endpoint: "0.0.0.0:8889" } },
];

function resolveTheme(mode: ThemeMode): "light" | "dark" {
  if (mode !== "system") return mode;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export default function App() {
  const [themeMode, setThemeMode] = useState<ThemeMode>(() => {
    const s = localStorage.getItem(THEME_STORAGE_KEY);
    return s === "dark" || s === "light" || s === "system" ? s : "system";
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [connectWarning, setConnectWarning] = useState<string | null>(null);

  const [host, setHost] = useState("");
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [verifyTls, setVerifyTls] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);

  const [apis, setApis] = useState<ApiRow[]>([]);
  const [modules, setModules] = useState<string[]>([]);
  const [metricsOnly, setMetricsOnly] = useState(true);
  const [moduleFilter, setModuleFilter] = useState("");
  const [selectedEndpoints, setSelectedEndpoints] = useState<Set<string>>(new Set());

  const [catalog, setCatalog] = useState<ExporterCatalogItem[]>([]);
  const [exporters, setExporters] = useState<ExporterConfig[]>(DEFAULT_EXPORTERS);
  const [collectorYaml, setCollectorYaml] = useState("");

  const [pollInterval, setPollInterval] = useState(30);
  const [otlpEndpoint, setOtlpEndpoint] = useState("http://127.0.0.1:4318");
  const [exportStatus, setExportStatus] = useState<Record<string, unknown> | null>(null);
  const [promHints, setPromHints] = useState<Record<string, string>>({});
  type PromControl = {
    reload_url?: string;
    restart_mode?: string;
    restart_available?: boolean;
    restart_hint?: string;
    reload_hint?: string;
    restart_hint_detail?: string;
  };
  const [promControl, setPromControl] = useState<PromControl | null>(null);
  const [promActionStatus, setPromActionStatus] = useState<string | null>(null);

  useEffect(() => {
    const resolved = resolveTheme(themeMode);
    document.documentElement.setAttribute("data-theme", resolved);
    localStorage.setItem(THEME_STORAGE_KEY, themeMode);
  }, [themeMode]);

  useEffect(() => {
    void (async () => {
      try {
        const [apiRes, catRes, promRes, runtimeRes, promCtrlRes] = await Promise.all([
          apiFetch("/api/apis?metrics_only=false"),
          apiFetch("/api/exporters/catalog"),
          apiFetch("/api/validation/prometheus"),
          apiFetch("/api/runtime-config"),
          apiFetch("/api/prometheus/control"),
        ]);
        const apiData = await readJson<{ apis: ApiRow[]; modules: string[] }>(apiRes);
        const catData = await readJson<{ exporters: ExporterCatalogItem[] }>(catRes);
        const promData = await readJson<Record<string, string>>(promRes);
        const promCtrl = await readJson<PromControl>(promCtrlRes);
        const runtime = await readJson<{ otlp_endpoint?: string }>(runtimeRes);
        setApis(apiData.apis);
        setModules(apiData.modules);
        setCatalog(catData.exporters);
        setPromHints(promData);
        setPromControl(promCtrl);
        if (runtime.otlp_endpoint) setOtlpEndpoint(runtime.otlp_endpoint);
        const defaults = apiData.apis
          .filter((a) => a.collect_metrics === "true")
          .map((a) => a.endpoint);
        setSelectedEndpoints(new Set(defaults));
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, []);

  const filteredApis = useMemo(() => {
    return apis.filter((a) => {
      if (metricsOnly && a.collect_metrics !== "true") return false;
      if (moduleFilter && (a.module || "").toUpperCase() !== moduleFilter) return false;
      return true;
    });
  }, [apis, metricsOnly, moduleFilter]);

  const connect = useCallback(async () => {
    setBusy(true);
    setError(null);
    setConnectWarning(null);
    try {
      const r = await apiFetch("/api/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          host,
          username,
          password,
          verify_tls: verifyTls,
        }),
      });
      const data = await readJson<{ session_id: string; warning?: string }>(r);
      setSessionId(data.session_id);
      setConnectWarning(data.warning ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [host, username, password, verifyTls]);

  const applyCollector = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await apiFetch("/api/collector/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ exporters }),
      });
      const data = await readJson<{ yaml: string; restart_command: string }>(r);
      setCollectorYaml(data.yaml);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [exporters]);

  const loadCollectorYaml = useCallback(async () => {
    const r = await apiFetch("/api/collector/config");
    const data = await readJson<{ yaml: string; exporters: ExporterConfig[] }>(r);
    setCollectorYaml(data.yaml);
    if (data.exporters?.length) setExporters(data.exporters);
  }, []);

  const startExport = useCallback(async () => {
    if (!sessionId) {
      setError("Connect to BIG-IP first");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const endpoints = Array.from(selectedEndpoints);
      const r = await apiFetch(`/api/export/start?session_id=${encodeURIComponent(sessionId)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          endpoints,
          metrics_only: metricsOnly,
          modules: moduleFilter ? [moduleFilter] : [],
          poll_interval_sec: pollInterval,
          otlp_endpoint: otlpEndpoint,
        }),
      });
      const data = await readJson<{ status: Record<string, unknown> }>(r);
      setExportStatus(data.status);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [sessionId, selectedEndpoints, metricsOnly, moduleFilter, pollInterval, otlpEndpoint]);

  const stopExport = useCallback(async () => {
    setBusy(true);
    try {
      await apiFetch("/api/export/stop", { method: "POST" });
      setExportStatus({ running: false });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  const refreshStatus = useCallback(async () => {
    const r = await apiFetch("/api/export/status");
    const data = await readJson<{ loop: Record<string, unknown> }>(r);
    setExportStatus(data.loop);
  }, []);

  const reloadPrometheus = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await apiFetch("/api/prometheus/reload", { method: "POST" });
      const data = await readJson<{ message?: string }>(r);
      setPromActionStatus(data.message ?? "Prometheus reloaded.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  const restartPrometheus = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await apiFetch("/api/prometheus/restart", { method: "POST" });
      const data = await readJson<{ message?: string }>(r);
      setPromActionStatus(data.message ?? "Prometheus restarted.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  const toggleEndpoint = (ep: string) => {
    setSelectedEndpoints((prev) => {
      const next = new Set(prev);
      if (next.has(ep)) next.delete(ep);
      else next.add(ep);
      return next;
    });
  };

  const addExporter = () => {
    const first = catalog.find((c) => c.type !== "prometheus")?.type ?? "debug";
    setExporters((prev) => [...prev, { type: first, enabled: true, params: {} }]);
  };

  const updateExporter = (idx: number, patch: Partial<ExporterConfig>) => {
    setExporters((prev) => prev.map((e, i) => (i === idx ? { ...e, ...patch } : e)));
  };

  const removeExporter = (idx: number) => {
    setExporters((prev) => prev.filter((_, i) => i !== idx));
  };

  return (
    <div className="app">
      {busy && (
        <div className="app-busy-overlay" aria-busy="true">
          <div className="app-busy-spinner" />
        </div>
      )}

      <header className="app-header">
        <div className="app-header-brand">
          <img src={f5LogoUrl} alt="F5" className="app-header-logo" />
          <div className="app-header-main">
            <h1 className="app-title">BIG-IP Metrics Exporter</h1>
            <p className="muted">
              Pull iControl REST metrics, push via OTLP to the OpenTelemetry Collector, validate in
              Prometheus.
            </p>
          </div>
        </div>
        <div className="theme-toolbar">
          <span className="theme-toolbar-label">Theme</span>
          <div className="theme-segment" role="group" aria-label="Theme">
            {(["light", "dark", "system"] as ThemeMode[]).map((m) => (
              <button
                key={m}
                type="button"
                aria-pressed={themeMode === m}
                onClick={() => setThemeMode(m)}
              >
                {m}
              </button>
            ))}
          </div>
        </div>
      </header>

      {error && <div className="banner-error">{error}</div>}
      {connectWarning && !error && (
        <div className="banner-error" style={{ background: "var(--status-not)", color: "var(--text)" }}>
          {connectWarning}
        </div>
      )}

      <section className="card">
        <h2>BIG-IP connection</h2>
        <div className="row">
          <div className="field">
            <label>Management host</label>
            <input value={host} onChange={(e) => setHost(e.target.value)} placeholder="10.0.0.1" />
          </div>
          <div className="field">
            <label>Username</label>
            <input value={username} onChange={(e) => setUsername(e.target.value)} />
          </div>
          <div className="field">
            <label>Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
        </div>
        <label className="check">
          <input
            type="checkbox"
            checked={verifyTls}
            onChange={(e) => setVerifyTls(e.target.checked)}
          />
          Verify TLS certificate
        </label>
        <div className="actions">
          <button type="button" className="btn btn-primary" onClick={() => void connect()}>
            Connect
          </button>
          {sessionId && (
            <span className="status-ready">Session active</span>
          )}
        </div>
      </section>

      <section className="card">
        <h2>API endpoints ({filteredApis.length})</h2>
        <p className="muted">
          Catalog from <code>data/bigip_apis.csv</code> (84 iControl REST paths). Select endpoints
          to poll; stats paths are recommended for metrics.
        </p>
        <div className="row">
          <label className="check">
            <input
              type="checkbox"
              checked={metricsOnly}
              onChange={(e) => setMetricsOnly(e.target.checked)}
            />
            Metrics / stats endpoints only
          </label>
          <div className="field">
            <label>Module filter</label>
            <select value={moduleFilter} onChange={(e) => setModuleFilter(e.target.value)}>
              <option value="">All modules</option>
              {modules.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="actions">
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() =>
              setSelectedEndpoints(new Set(filteredApis.map((a) => a.endpoint)))
            }
          >
            Select all visible
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => setSelectedEndpoints(new Set())}
          >
            Clear selection
          </button>
        </div>
        <div className="api-table-wrap">
          <table className="api-table">
            <thead>
              <tr>
                <th />
                <th>Module</th>
                <th>Endpoint</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              {filteredApis.map((a) => (
                <tr key={a.endpoint}>
                  <td>
                    <input
                      type="checkbox"
                      checked={selectedEndpoints.has(a.endpoint)}
                      onChange={() => toggleEndpoint(a.endpoint)}
                    />
                  </td>
                  <td>{a.module}</td>
                  <td>
                    <code>{a.endpoint}</code>
                  </td>
                  <td>{a.description}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="muted">{selectedEndpoints.size} endpoint(s) selected</p>
      </section>

      <section className="card">
        <h2>OpenTelemetry Collector exporters</h2>
        <p className="muted">
          Configure collector export pipelines. A Prometheus exporter on port 8889 is always
          included for local validation. After applying, restart the collector:{" "}
          <code>docker compose restart otel-collector</code>
        </p>
        {exporters.map((exp, idx) => (
          <div key={idx} className="exporter-row">
            <div className="row">
              <div className="field">
                <label>Exporter type</label>
                <select
                  value={exp.type}
                  onChange={(e) => updateExporter(idx, { type: e.target.value, params: {} })}
                >
                  {catalog.map((c) => (
                    <option key={c.type} value={c.type}>
                      {c.label}
                    </option>
                  ))}
                </select>
              </div>
              <label className="check">
                <input
                  type="checkbox"
                  checked={exp.enabled}
                  onChange={(e) => updateExporter(idx, { enabled: e.target.checked })}
                />
                Enabled
              </label>
            </div>
            {exp.type === "otlp_http" && (
              <div className="field">
                <label>OTLP HTTP endpoint (base URL)</label>
                <input
                  value={String(exp.params.endpoint ?? "http://localhost:4318")}
                  onChange={(e) =>
                    updateExporter(idx, { params: { ...exp.params, endpoint: e.target.value } })
                  }
                />
              </div>
            )}
            {exp.type === "otlp_grpc" && (
              <div className="field">
                <label>OTLP gRPC endpoint (host:port)</label>
                <input
                  value={String(exp.params.endpoint ?? "localhost:4317")}
                  onChange={(e) =>
                    updateExporter(idx, { params: { ...exp.params, endpoint: e.target.value } })
                  }
                />
              </div>
            )}
            {exp.type === "file" && (
              <div className="field">
                <label>File path (inside collector container)</label>
                <input
                  value={String(exp.params.path ?? "/tmp/bigip-metrics.json")}
                  onChange={(e) =>
                    updateExporter(idx, { params: { ...exp.params, path: e.target.value } })
                  }
                />
              </div>
            )}
            <button type="button" className="btn btn-secondary" onClick={() => removeExporter(idx)}>
              Remove
            </button>
          </div>
        ))}
        <div className="actions">
          <button type="button" className="btn btn-secondary" onClick={addExporter}>
            Add exporter
          </button>
          <button type="button" className="btn btn-primary" onClick={() => void applyCollector()}>
            Apply collector config
          </button>
          <button type="button" className="btn btn-secondary" onClick={() => void loadCollectorYaml()}>
            Reload YAML
          </button>
        </div>
        {collectorYaml && (
          <pre className="report-pre">{collectorYaml}</pre>
        )}
      </section>

      <section className="card">
        <h2>Export to collector (OTLP)</h2>
        <div className="row">
          <div className="field">
            <label>OTLP HTTP endpoint (Python → collector)</label>
            <input value={otlpEndpoint} onChange={(e) => setOtlpEndpoint(e.target.value)} />
          </div>
          <div className="field">
            <label>Poll interval (seconds)</label>
            <input
              type="number"
              min={5}
              value={pollInterval}
              onChange={(e) => setPollInterval(Number(e.target.value))}
            />
          </div>
        </div>
        <div className="actions">
          <button type="button" className="btn btn-primary" onClick={() => void startExport()}>
            Start export
          </button>
          <button type="button" className="btn btn-danger" onClick={() => void stopExport()}>
            Stop export
          </button>
          <button type="button" className="btn btn-secondary" onClick={() => void refreshStatus()}>
            Refresh status
          </button>
        </div>
        {exportStatus && (
          <pre className="report-pre">{JSON.stringify(exportStatus, null, 2)}</pre>
        )}
      </section>

      <section className="card">
        <h2>Prometheus validation</h2>
        <p className="muted">
          Start the stack with <code>docker compose up -d</code>, then confirm metrics in
          Prometheus.
        </p>
        <ul>
          <li>
            <a href={promHints.prometheus_ui ?? "http://127.0.0.1:9090"} target="_blank" rel="noreferrer">
              Prometheus UI
            </a>
          </li>
          <li>
            <a
              href={promHints.collector_metrics ?? "http://127.0.0.1:8889/metrics"}
              target="_blank"
              rel="noreferrer"
            >
              Collector Prometheus exporter
            </a>
          </li>
        </ul>
        <p className="muted">
          Example query: <code>{promHints.query_example ?? "bigip_"}</code>
        </p>
        <p className="muted">
          {promControl?.reload_hint ??
            "Reload picks up config/target changes. Restart recycles the Prometheus process."}
        </p>
        <div className="actions">
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => void reloadPrometheus()}
            title="POST /-/reload — refresh scrape config and targets"
          >
            Reload Prometheus
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => void restartPrometheus()}
            title={promControl?.restart_hint ?? "Restart Prometheus container/pod"}
          >
            Restart Prometheus
          </button>
        </div>
        {promControl && !promControl.restart_available && promControl.restart_hint && (
          <p className="muted">
            Full restart from the API is unavailable here. Use <strong>Reload</strong>, or run:{" "}
            <code>{promControl.restart_hint}</code>
          </p>
        )}
        {promActionStatus && (
          <p className="status-ready">{promActionStatus}</p>
        )}
      </section>
    </div>
  );
}
