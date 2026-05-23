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

type ExporterFieldSpec = {
  name: string;
  label: string;
  field_type?: string;
  default?: string | number | boolean;
  required?: boolean;
  placeholder?: string;
  options?: string[];
};

type ExporterCatalogItem = {
  type: string;
  label: string;
  description: string;
  category?: string;
  component?: string;
  doc_url?: string;
  fields?: ExporterFieldSpec[];
};

type ContribComponent = {
  component: string;
  folder: string;
  doc_url: string;
};

type ExporterConfig = {
  type: string;
  enabled: boolean;
  params: Record<string, string | number | boolean>;
};

function defaultParamsForSpec(spec: ExporterCatalogItem | undefined): Record<string, string | number | boolean> {
  if (!spec?.fields?.length) return {};
  const out: Record<string, string | number | boolean> = {};
  for (const f of spec.fields) {
    if (f.default !== undefined && f.default !== "") {
      out[f.name] = f.default;
    }
  }
  return out;
}

type BigIPDevice = {
  session_id: string;
  host: string;
  display_host: string;
  label: string;
  token_timeout_sec?: number;
  warning?: string | null;
  request_log_profile?: string | null;
  request_log_profile_created?: boolean | null;
  asm_log_profile?: string | null;
  asm_log_profile_created?: boolean | null;
  afm_log_profile?: string | null;
  afm_log_profile_created?: boolean | null;
  http_analytics_profile?: string | null;
  http_analytics_profile_created?: boolean | null;
  tcp_analytics_profile?: string | null;
  tcp_analytics_profile_created?: boolean | null;
  export_metrics?: boolean;
  export_logs?: boolean;
  connected_since?: number;
};

function exportModeLabel(device: BigIPDevice): string {
  const metrics = device.export_metrics !== false;
  const logs = device.export_logs !== false;
  if (metrics && logs) return "Metrics + logs";
  if (metrics) return "Metrics only";
  if (logs) return "Logs only";
  return "None";
}

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

function connectedCountLabel(count: number): string {
  if (count === 0) return "No BIG-IPs connected";
  return `${count} BIG-IP${count === 1 ? "" : "s"} connected`;
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
  const [deviceLabel, setDeviceLabel] = useState("");
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [verifyTls, setVerifyTls] = useState(false);
  const [connectExportMetrics, setConnectExportMetrics] = useState(true);
  const [connectExportLogs, setConnectExportLogs] = useState(true);
  const [devices, setDevices] = useState<BigIPDevice[]>([]);
  const [exportDeviceIds, setExportDeviceIds] = useState<Set<string>>(new Set());

  const [apis, setApis] = useState<ApiRow[]>([]);
  const [metricsOnly, setMetricsOnly] = useState(true);
  const [moduleFilter, setModuleFilter] = useState("");
  const [selectedEndpoints, setSelectedEndpoints] = useState<Set<string>>(new Set());

  const [catalog, setCatalog] = useState<ExporterCatalogItem[]>([]);
  const [catalogCategories, setCatalogCategories] = useState<string[]>([]);
  const [contribComponents, setContribComponents] = useState<ContribComponent[]>([]);
  const [contribRepoUrl, setContribRepoUrl] = useState(
    "https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter",
  );
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
    wipe_tsdb_on_reload?: boolean;
    wipe_tsdb_available?: boolean;
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

  const refreshDevices = useCallback(async () => {
    const r = await apiFetch("/api/bigips");
    const data = await readJson<{ devices: BigIPDevice[] }>(r);
    setDevices(data.devices);
    setExportDeviceIds((prev) => {
      const ids = new Set(data.devices.map((d) => d.session_id));
      if (prev.size === 0) return ids;
      const next = new Set<string>();
      for (const id of prev) {
        if (ids.has(id)) next.add(id);
      }
      return next.size > 0 ? next : ids;
    });
  }, []);

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
        const apiData = await readJson<{
          apis: ApiRow[];
          modules?: string[];
          module_counts?: Record<string, number>;
        }>(apiRes);
        const catData = await readJson<{
          exporters: ExporterCatalogItem[];
          categories?: string[];
          contrib_components?: ContribComponent[];
          contrib_repo_url?: string;
        }>(catRes);
        const promData = await readJson<Record<string, string>>(promRes);
        const promCtrl = await readJson<PromControl>(promCtrlRes);
        const runtime = await readJson<{ otlp_endpoint?: string }>(runtimeRes);
        setApis(apiData.apis);
        setCatalog(catData.exporters);
        setCatalogCategories(catData.categories ?? []);
        setContribComponents(catData.contrib_components ?? []);
        if (catData.contrib_repo_url) setContribRepoUrl(catData.contrib_repo_url);
        setPromHints(promData);
        setPromControl(promCtrl);
        if (runtime.otlp_endpoint) setOtlpEndpoint(runtime.otlp_endpoint);
        const defaults = apiData.apis
          .filter((a) => a.collect_metrics === "true")
          .map((a) => a.endpoint);
        setSelectedEndpoints(new Set(defaults));
        await refreshDevices();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, [refreshDevices]);

  useEffect(() => {
    const id = window.setInterval(() => {
      void refreshDevices();
    }, 45_000);
    return () => window.clearInterval(id);
  }, [refreshDevices]);

  const exportSelectedDevices = useMemo(() => {
    return devices.filter((d) => exportDeviceIds.has(d.session_id));
  }, [devices, exportDeviceIds]);

  const moduleFilterOptions = useMemo(() => {
    const counts = new Map<string, number>();
    for (const a of apis) {
      const mod = (a.module || "").trim().toUpperCase();
      if (!mod) continue;
      counts.set(mod, (counts.get(mod) ?? 0) + 1);
    }
    return Array.from(counts.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([mod, total]) => ({ mod, total }));
  }, [apis]);

  const filteredApis = useMemo(() => {
    return apis.filter((a) => {
      if (metricsOnly && a.collect_metrics !== "true") return false;
      const mod = (a.module || "").trim().toUpperCase();
      if (moduleFilter && mod !== moduleFilter) return false;
      return true;
    });
  }, [apis, metricsOnly, moduleFilter]);

  const filteredMetricsCount = useMemo(
    () => filteredApis.filter((a) => a.collect_metrics === "true").length,
    [filteredApis],
  );

  const canConnect = useMemo(
    () =>
      Boolean(
        host.trim() &&
          username.trim() &&
          password &&
          (connectExportMetrics || connectExportLogs),
      ),
    [host, username, password, connectExportMetrics, connectExportLogs],
  );

  const connect = useCallback(async () => {
    if (!canConnect) {
      setError(
        "Management host, username, password, and at least one of metrics or logs are required",
      );
      return;
    }
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
          label: deviceLabel.trim(),
          export_metrics: connectExportMetrics,
          export_logs: connectExportLogs,
        }),
      });
      const data = await readJson<{
        session_id: string;
        warning?: string;
        export_metrics?: boolean;
      }>(r);
      setConnectWarning(data.warning ?? null);
      setHost("");
      setDeviceLabel("");
      setUsername("admin");
      setPassword("");
      await refreshDevices();
      if (connectExportMetrics) {
        setExportDeviceIds((prev) => new Set(prev).add(data.session_id));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [
    canConnect,
    host,
    deviceLabel,
    username,
    password,
    verifyTls,
    connectExportMetrics,
    connectExportLogs,
    refreshDevices,
  ]);

  const disconnectDevice = useCallback(
    async (sessionId: string) => {
      setBusy(true);
      setError(null);
      try {
        await apiFetch(`/api/session/${encodeURIComponent(sessionId)}`, {
          method: "DELETE",
        });
        await refreshDevices();
        setExportDeviceIds((prev) => {
          const next = new Set(prev);
          next.delete(sessionId);
          return next;
        });
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [refreshDevices],
  );

  const toggleExportDevice = (sessionId: string) => {
    setExportDeviceIds((prev) => {
      const next = new Set(prev);
      if (next.has(sessionId)) next.delete(sessionId);
      else next.add(sessionId);
      return next;
    });
  };

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
    if (devices.length === 0) {
      setError("Connect to at least one BIG-IP first");
      return;
    }
    if (exportDeviceIds.size === 0) {
      setError("Select at least one BIG-IP device for export");
      return;
    }
    const sessionIds = devices
      .filter((d) => exportDeviceIds.has(d.session_id) && d.export_metrics !== false)
      .map((d) => d.session_id);
    if (sessionIds.length === 0) {
      setError(
        "No checked devices have metrics export enabled. Connect with Export metrics or change selection.",
      );
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const endpoints = Array.from(selectedEndpoints);
      const r = await apiFetch("/api/export/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_ids: sessionIds,
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
  }, [
    devices,
    exportDeviceIds,
    selectedEndpoints,
    metricsOnly,
    moduleFilter,
    pollInterval,
    otlpEndpoint,
  ]);

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
    await refreshDevices();
    const r = await apiFetch("/api/export/status");
    const data = await readJson<{
      loop: Record<string, unknown>;
      connected_devices?: BigIPDevice[];
    }>(r);
    setExportStatus(data.loop);
    if (data.connected_devices?.length !== undefined) {
      setDevices(data.connected_devices);
    }
  }, [refreshDevices]);

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

  const catalogByType = useMemo(() => {
    const m = new Map<string, ExporterCatalogItem>();
    for (const c of catalog) m.set(c.type, c);
    return m;
  }, [catalog]);

  const addExporter = () => {
    const first = catalog.find((c) => c.type === "otlp_http") ?? catalog[0];
    if (!first) return;
    setExporters((prev) => [
      ...prev,
      { type: first.type, enabled: true, params: defaultParamsForSpec(first) },
    ]);
  };

  const updateExporter = (idx: number, patch: Partial<ExporterConfig>) => {
    setExporters((prev) => prev.map((e, i) => (i === idx ? { ...e, ...patch } : e)));
  };

  const setExporterType = (idx: number, newType: string) => {
    const spec = catalogByType.get(newType);
    updateExporter(idx, { type: newType, params: defaultParamsForSpec(spec) });
  };

  const setExporterParam = (
    idx: number,
    name: string,
    value: string | number | boolean,
  ) => {
    setExporters((prev) =>
      prev.map((e, i) =>
        i === idx ? { ...e, params: { ...e.params, [name]: value } } : e,
      ),
    );
  };

  const renderExporterFields = (exp: ExporterConfig, idx: number) => {
    const spec = catalogByType.get(exp.type);
    if (!spec?.fields?.length) return null;

    return spec.fields.map((field) => {
      const value = exp.params[field.name] ?? field.default ?? "";
      const id = `exp-${idx}-${field.name}`;

      if (field.field_type === "bool") {
        return (
          <label key={field.name} className="check">
            <input
              id={id}
              type="checkbox"
              checked={Boolean(value)}
              onChange={(e) => setExporterParam(idx, field.name, e.target.checked)}
            />
            {field.label}
          </label>
        );
      }

      if (field.field_type === "select" && field.options?.length) {
        return (
          <div key={field.name} className="field">
            <label htmlFor={id}>{field.label}</label>
            <select
              id={id}
              value={String(value)}
              onChange={(e) => setExporterParam(idx, field.name, e.target.value)}
            >
              {field.options.map((opt) => (
                <option key={opt} value={opt}>
                  {opt}
                </option>
              ))}
            </select>
          </div>
        );
      }

      if (field.field_type === "textarea") {
        return (
          <div key={field.name} className="field field-full">
            <label htmlFor={id}>{field.label}</label>
            <textarea
              id={id}
              rows={5}
              value={String(value)}
              placeholder={field.placeholder}
              onChange={(e) => setExporterParam(idx, field.name, e.target.value)}
            />
          </div>
        );
      }

      if (field.name === "component" && exp.type === "contrib") {
        return (
          <div key={field.name} className="field">
            <label htmlFor={id}>{field.label}</label>
            <input
              id={id}
              list={`contrib-components-${idx}`}
              value={String(value)}
              placeholder={field.placeholder}
              onChange={(e) => setExporterParam(idx, field.name, e.target.value)}
            />
            <datalist id={`contrib-components-${idx}`}>
              {contribComponents.map((c) => (
                <option key={c.component} value={c.component}>
                  {c.folder}
                </option>
              ))}
            </datalist>
          </div>
        );
      }

      return (
        <div key={field.name} className="field">
          <label htmlFor={id}>{field.label}</label>
          <input
            id={id}
            type={field.field_type === "password" ? "password" : field.field_type === "number" ? "number" : "text"}
            value={String(value)}
            placeholder={field.placeholder}
            onChange={(e) =>
              setExporterParam(
                idx,
                field.name,
                field.field_type === "number" ? Number(e.target.value) : e.target.value,
              )
            }
          />
        </div>
      );
    });
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

      {devices.length > 0 && (
        <section className="connected-status-bar" aria-live="polite">
          <div className="connected-status-summary">
            <span
              className="connected-badge connected-badge-active"
              aria-label={connectedCountLabel(devices.length)}
            >
              {devices.length}
            </span>
            <div className="connected-status-text">
              <strong>{connectedCountLabel(devices.length)}</strong>
              <span className="muted connected-status-sub">
                {exportSelectedDevices.length} selected for export
              </span>
            </div>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => void refreshDevices()}
            >
              Refresh list
            </button>
          </div>
          <ul className="connected-chips" aria-label="Connected BIG-IP devices">
            {devices.map((d) => (
              <li key={d.session_id} className="connected-chip">
                <span className="connected-chip-label">{d.label || d.display_host}</span>
                <span className="connected-chip-host">{d.display_host}</span>
                {exportDeviceIds.has(d.session_id) && (
                  <span className="connected-chip-export" title="Included in export">
                    export
                  </span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="card">
        <h2>BIG-IP connections ({devices.length} connected)</h2>
        <p className="muted">
          Connect one or more management addresses. Metrics are tagged per device (
          <code>bigip.host</code>). Reconnecting the same host replaces the previous session.
        </p>
        <h3 className="subsection-title">Currently connected</h3>
        {devices.length === 0 ? (
          <p className="muted device-list-empty">No BIG-IPs connected. Use the form below to connect.</p>
        ) : (
          <ul className="device-list">
            {devices.map((d) => (
              <li key={d.session_id} className="device-list-item">
                <label className="check device-list-check">
                  <input
                    type="checkbox"
                    checked={exportDeviceIds.has(d.session_id)}
                    onChange={() => toggleExportDevice(d.session_id)}
                    title="Include in export"
                  />
                  <span>
                    <strong>{d.label || d.display_host}</strong>
                    <span className="muted device-list-host"> — {d.display_host}</span>
                    <span className="muted device-list-export-mode" title="Connect options">
                      {" "}
                      ({exportModeLabel(d)})
                    </span>
                  </span>
                </label>
                {d.warning && <span className="device-list-warn">{d.warning}</span>}
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={() => void disconnectDevice(d.session_id)}
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}
        <h3 className="subsection-title">
          {devices.length > 0 ? "Add another BIG-IP" : "Connect BIG-IP"}
        </h3>
        <div className="row">
          <div className="field">
            <label>Management host</label>
            <input value={host} onChange={(e) => setHost(e.target.value)} placeholder="10.0.0.1" />
          </div>
          <div className="field">
            <label>Label (optional)</label>
            <input
              value={deviceLabel}
              onChange={(e) => setDeviceLabel(e.target.value)}
              placeholder="e.g. prod-dc1"
            />
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
        <div className="connect-export-options">
          <span className="connect-export-options-label">On connect</span>
          <label className="check">
            <input
              type="checkbox"
              checked={connectExportMetrics}
              onChange={(e) => setConnectExportMetrics(e.target.checked)}
            />
            Export metrics
          </label>
          <label className="check">
            <input
              type="checkbox"
              checked={connectExportLogs}
              onChange={(e) => setConnectExportLogs(e.target.checked)}
            />
            Export logs (create logging profiles)
          </label>
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
          <button
            type="button"
            className="btn btn-primary"
            disabled={!canConnect || busy}
            onClick={() => void connect()}
            title={
              canConnect
                ? undefined
                : "Enter host, credentials, and select metrics and/or logs"
            }
          >
            {devices.length > 0 ? "Add BIG-IP" : "Connect"}
          </button>
          <span className={devices.length > 0 ? "status-ready" : "muted"}>
            {connectedCountLabel(devices.length)}
          </span>
        </div>
      </section>

      <section className="card">
        <h2>API endpoints ({filteredApis.length})</h2>
        <p className="muted">
          Catalog from <code>data/bigip_apis.csv</code> ({apis.length} iControl REST paths). Select endpoints
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
              {moduleFilterOptions.map(({ mod, total }) => (
                <option key={mod} value={mod}>
                  {mod} ({total})
                </option>
              ))}
            </select>
          </div>
        </div>
        {moduleFilter ? (
          <p className="muted">
            Showing {filteredApis.length} path(s) for module <code>{moduleFilter}</code>
            {metricsOnly
              ? ` (${filteredMetricsCount} metrics-oriented)`
              : ` (${filteredMetricsCount} with collect_metrics enabled)`}
          </p>
        ) : null}
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
          Forward metrics to{" "}
          <a href={contribRepoUrl} target="_blank" rel="noreferrer">
            OpenTelemetry Collector Contrib
          </a>{" "}
          exporters. A Prometheus exporter on port 8889 is always included for local validation.
          After applying, restart the collector:{" "}
          <code>docker compose restart otel-collector</code>
        </p>
        {exporters.map((exp, idx) => {
          const spec = catalogByType.get(exp.type);
          return (
            <div key={idx} className="exporter-row">
              <div className="row">
                <div className="field">
                  <label>Exporter type</label>
                  <select
                    value={exp.type}
                    onChange={(e) => setExporterType(idx, e.target.value)}
                  >
                    {(catalogCategories.length > 0
                      ? catalogCategories
                      : [...new Set(catalog.map((c) => c.category ?? "Other"))]
                    ).map((cat) => (
                      <optgroup key={cat} label={cat}>
                        {catalog
                          .filter((c) => (c.category ?? "Other") === cat)
                          .map((c) => (
                            <option key={c.type} value={c.type}>
                              {c.label}
                            </option>
                          ))}
                      </optgroup>
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
              {spec?.description && (
                <p className="muted exporter-desc">
                  {spec.description}
                  {spec.doc_url && (
                    <>
                      {" "}
                      <a href={spec.doc_url} target="_blank" rel="noreferrer">
                        Docs
                      </a>
                    </>
                  )}
                </p>
              )}
              <div className="exporter-fields">{renderExporterFields(exp, idx)}</div>
              <button type="button" className="btn btn-secondary" onClick={() => removeExporter(idx)}>
                Remove
              </button>
            </div>
          );
        })}
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
        <p className="muted">
          Polls the checked BIG-IP devices in the connections list above (
          {exportSelectedDevices.length} of {devices.length} connected).
        </p>
        {devices.length > 0 && (
          <ul className="connected-chips connected-chips-compact" aria-label="BIG-IPs in export">
            {exportSelectedDevices.length === 0 ? (
              <li className="muted">No devices checked for export.</li>
            ) : (
              exportSelectedDevices.map((d) => (
                <li key={d.session_id} className="connected-chip">
                  <span className="connected-chip-label">{d.label || d.display_host}</span>
                  <span className="connected-chip-host">{d.display_host}</span>
                </li>
              ))
            )}
          </ul>
        )}
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
            title="Wipe TSDB, restart Prometheus, reload scrape config"
          >
            Reload Prometheus (wipe data)
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
