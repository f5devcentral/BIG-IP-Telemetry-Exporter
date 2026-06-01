import { useCallback, useEffect, useMemo, useState, type Dispatch, type SetStateAction } from "react";
import f5LogoUrl from "../../F5-logo-F5-rgb.svg";

const THEME_STORAGE_KEY = "bigip-telemetry-ui-theme";
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
  signals?: string[];
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

type ExporterSignal = "metrics" | "logs";

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

function defaultParamsForSignal(
  spec: ExporterCatalogItem | undefined,
  signal: ExporterSignal,
): Record<string, string | number | boolean> {
  const params = defaultParamsForSpec(spec);
  if (signal === "logs" && spec?.type === "file" && !params.path) {
    return { ...params, path: "/tmp/bigip-logs.json" };
  }
  return params;
}

function catalogForSignal(catalog: ExporterCatalogItem[], signal: ExporterSignal): ExporterCatalogItem[] {
  return catalog.filter((c) => (c.signals ?? ["metrics"]).includes(signal));
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
  log_syslog_target?: string | null;
  log_hsl_target?: string | null;
  system_syslog_target?: string | null;
  export_metrics?: boolean;
  export_logs?: boolean;
  export_system_logs?: boolean;
  export_ltm_logs?: boolean;
  export_asm_logs?: boolean;
  export_afm_logs?: boolean;
  export_avr_logs?: boolean;
  prov_ltm?: boolean;
  prov_asm?: boolean;
  prov_afm?: boolean;
  prov_avr?: boolean;
  connected_since?: number;
};

function exportModeLabel(device: BigIPDevice): string {
  const metrics = device.export_metrics !== false;
  const logs = deviceExportsLogs(device);
  if (metrics && logs) return "Metrics + logs";
  if (metrics) return "Metrics only";
  if (logs) return "Logs only";
  return "None";
}

function deviceHasModuleLogsActive(device: BigIPDevice): boolean {
  if (!device.export_logs) return false;
  return (
    device.export_ltm_logs !== false ||
    device.export_asm_logs !== false ||
    device.export_afm_logs !== false ||
    device.export_avr_logs !== false
  );
}

function deviceExportsLogs(device: BigIPDevice): boolean {
  return deviceHasModuleLogsActive(device) || Boolean(device.export_system_logs);
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

const DEFAULT_METRIC_EXPORTERS: ExporterConfig[] = [];

const DEFAULT_LOG_EXPORTERS: ExporterConfig[] = [
  { type: "debug", enabled: true, params: { verbosity: "basic" } },
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
  const [metricExporters, setMetricExporters] = useState<ExporterConfig[]>(DEFAULT_METRIC_EXPORTERS);
  const [logExporters, setLogExporters] = useState<ExporterConfig[]>(DEFAULT_LOG_EXPORTERS);
  const [collectorYaml, setCollectorYaml] = useState("");
  const [collectorActionStatus, setCollectorActionStatus] = useState<string | null>(null);

  const [pollInterval, setPollInterval] = useState(30);
  const [otlpEndpoint, setOtlpEndpoint] = useState("http://127.0.0.1:4318");
  const [exportStatus, setExportStatus] = useState<Record<string, unknown> | null>(null);

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
        const [apiRes, catRes, runtimeRes] = await Promise.all([
          apiFetch("/api/apis?metrics_only=false"),
          apiFetch("/api/exporters/catalog"),
          apiFetch("/api/runtime-config"),
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
        const runtime = await readJson<{ otlp_endpoint?: string }>(runtimeRes);
        setApis(apiData.apis);
        setCatalog(catData.exporters);
        setCatalogCategories(catData.categories ?? []);
        setContribComponents(catData.contrib_components ?? []);
        if (catData.contrib_repo_url) setContribRepoUrl(catData.contrib_repo_url);
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

  const collectorExportModes = useMemo(() => {
    const src = exportSelectedDevices.length > 0 ? exportSelectedDevices : devices;
    if (src.length === 0) return { metrics: true, logs: true };
    return {
      metrics: src.some((d) => d.export_metrics !== false),
      logs: src.some((d) => deviceExportsLogs(d)),
    };
  }, [exportSelectedDevices, devices]);

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
          export_ltm_logs: connectExportLogs,
          export_asm_logs: connectExportLogs,
          export_afm_logs: connectExportLogs,
          export_avr_logs: connectExportLogs,
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

  const updateDeviceLogOptions = useCallback(
    async (
      sessionId: string,
      patch: Partial<Pick<
        BigIPDevice,
        "export_system_logs" | "export_ltm_logs" | "export_asm_logs" | "export_afm_logs" | "export_avr_logs"
      >>,
    ) => {
      setBusy(true);
      setError(null);
      try {
        await apiFetch(`/api/session/${encodeURIComponent(sessionId)}/log-options`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(patch),
        });
        await refreshDevices();
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
    setCollectorActionStatus(null);
    try {
      const r = await apiFetch("/api/collector/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          metric_exporters: metricExporters,
          log_exporters: logExporters,
          export_metrics:
            collectorExportModes.metrics || metricExporters.some((e) => e.enabled),
          export_logs: collectorExportModes.logs || logExporters.some((e) => e.enabled),
        }),
      });
      const data = await readJson<{
        yaml: string;
        collector_restart?: {
          attempted?: boolean;
          ok?: boolean;
          message?: string;
          error?: string;
          manual_hint?: string;
          skipped?: boolean;
        };
      }>(r);
      setCollectorYaml(data.yaml);
      const restart = data.collector_restart;
      if (restart?.ok && restart.message) {
        setCollectorActionStatus(restart.message);
      } else if (restart?.attempted && restart.error) {
        setCollectorActionStatus(
          `Config saved, but collector restart failed: ${restart.error}${
            restart.manual_hint ? ` Try: ${restart.manual_hint}` : ""
          }`,
        );
      } else if (restart?.skipped && restart.manual_hint) {
        setCollectorActionStatus(`Config saved. Restart manually: ${restart.manual_hint}`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [metricExporters, logExporters, collectorExportModes]);

  const loadCollectorYaml = useCallback(async () => {
    const r = await apiFetch("/api/collector/config");
    const data = await readJson<{
      yaml: string;
      metric_exporters?: ExporterConfig[];
      log_exporters?: ExporterConfig[];
      exporters?: ExporterConfig[];
    }>(r);
    setCollectorYaml(data.yaml);
    if (data.metric_exporters?.length) setMetricExporters(data.metric_exporters);
    else if (data.exporters?.length) setMetricExporters(data.exporters);
    if (data.log_exporters?.length) setLogExporters(data.log_exporters);
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
      .filter(
        (d) =>
          exportDeviceIds.has(d.session_id) &&
          (d.export_metrics !== false || deviceExportsLogs(d)),
      )
      .map((d) => d.session_id);
    if (sessionIds.length === 0) {
      setError(
        "No checked devices have metrics or log export enabled. Connect with Export metrics and/or Export logs.",
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
      const data = await readJson<{
        status?: Record<string, unknown>;
        log_forwarding?: Record<string, unknown>;
        mode?: string;
      }>(r);
      setExportStatus({
        ...(data.status ?? {}),
        log_forwarding: data.log_forwarding,
        mode: data.mode,
      });
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
      log_forwarding?: Record<string, unknown>;
      connected_devices?: BigIPDevice[];
    }>(r);
    setExportStatus({
      ...data.loop,
      log_forwarding: data.log_forwarding,
    });
    if (data.connected_devices?.length !== undefined) {
      setDevices(data.connected_devices);
    }
  }, [refreshDevices]);

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

  const addExporter = (
    signal: ExporterSignal,
    setList: Dispatch<SetStateAction<ExporterConfig[]>>,
  ) => {
    const filtered = catalogForSignal(catalog, signal);
    const preferred =
      signal === "metrics"
        ? filtered.find((c) => c.type === "otlp_http")
        : filtered.find((c) => c.type === "debug");
    const first = preferred ?? filtered[0];
    if (!first) return;
    setList((prev) => [
      ...prev,
      {
        type: first.type,
        enabled: true,
        params: defaultParamsForSignal(first, signal),
      },
    ]);
  };

  const updateExporter = (
    idx: number,
    patch: Partial<ExporterConfig>,
    setList: Dispatch<SetStateAction<ExporterConfig[]>>,
  ) => {
    setList((prev) => prev.map((e, i) => (i === idx ? { ...e, ...patch } : e)));
  };

  const setExporterType = (
    idx: number,
    newType: string,
    signal: ExporterSignal,
    setList: Dispatch<SetStateAction<ExporterConfig[]>>,
  ) => {
    const spec = catalogByType.get(newType);
    updateExporter(idx, { type: newType, params: defaultParamsForSignal(spec, signal) }, setList);
  };

  const setExporterParam = (
    idx: number,
    name: string,
    value: string | number | boolean,
    setList: Dispatch<SetStateAction<ExporterConfig[]>>,
  ) => {
    setList((prev) =>
      prev.map((e, i) =>
        i === idx ? { ...e, params: { ...e.params, [name]: value } } : e,
      ),
    );
  };

  const removeExporter = (
    idx: number,
    setList: Dispatch<SetStateAction<ExporterConfig[]>>,
  ) => {
    setList((prev) => prev.filter((_, i) => i !== idx));
  };

  const renderExporterFields = (
    exp: ExporterConfig,
    idx: number,
    setList: Dispatch<SetStateAction<ExporterConfig[]>>,
  ) => {
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
              onChange={(e) => setExporterParam(idx, field.name, e.target.checked, setList)}
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
              onChange={(e) => setExporterParam(idx, field.name, e.target.value, setList)}
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
              onChange={(e) => setExporterParam(idx, field.name, e.target.value, setList)}
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
              onChange={(e) => setExporterParam(idx, field.name, e.target.value, setList)}
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
                setList,
              )
            }
          />
        </div>
      );
    });
  };

  const renderExporterSection = (
    signal: ExporterSignal,
    title: string,
    hint: string,
    exporters: ExporterConfig[],
    setList: Dispatch<SetStateAction<ExporterConfig[]>>,
  ) => {
    const filteredCatalog = catalogForSignal(catalog, signal);
    const categories =
      catalogCategories.length > 0
        ? catalogCategories
        : [...new Set(filteredCatalog.map((c) => c.category ?? "Other"))];

    return (
      <div className="exporter-pipeline-section">
        <h3>{title}</h3>
        <p className="muted">{hint}</p>
        {exporters.map((exp, idx) => {
          const spec = catalogByType.get(exp.type);
          return (
            <div key={`${signal}-${idx}`} className="exporter-row">
              <div className="row">
                <div className="field">
                  <label>Exporter type</label>
                  <select
                    value={exp.type}
                    onChange={(e) => setExporterType(idx, e.target.value, signal, setList)}
                  >
                    {categories.map((cat) => (
                      <optgroup key={cat} label={cat}>
                        {filteredCatalog
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
                    onChange={(e) => updateExporter(idx, { enabled: e.target.checked }, setList)}
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
              <div className="exporter-fields">{renderExporterFields(exp, idx, setList)}</div>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => removeExporter(idx, setList)}
              >
                Remove
              </button>
            </div>
          );
        })}
        <div className="actions">
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => addExporter(signal, setList)}
          >
            Add {signal} exporter
          </button>
        </div>
      </div>
    );
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
            <h1 className="app-title">BIG-IP Telemetry Exporter</h1>
            <p className="muted">
              Export BIG-IP metrics and logs via OpenTelemetry Collector; validate metrics in
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
          <code>bigip.host</code>). After connect, use per-device toggles for LTM/ASM/AFM/AVR logs and
          system syslog. Reconnecting the same host replaces the previous session.
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
                {d.export_logs && (
                <div className="device-list-options">
                  {(d.prov_ltm || d.prov_asm || d.prov_afm || d.prov_avr) && (
                    <div className="device-list-options-row">
                      <span className="muted">Logs</span>
                      {d.prov_ltm && (
                        <label className="check">
                          <input
                            type="checkbox"
                            checked={d.export_ltm_logs !== false}
                            onChange={(e) =>
                              void updateDeviceLogOptions(d.session_id, { export_ltm_logs: e.target.checked })
                            }
                          />
                          LTM
                        </label>
                      )}
                      {d.prov_asm && (
                        <label className="check">
                          <input
                            type="checkbox"
                            checked={d.export_asm_logs !== false}
                            onChange={(e) =>
                              void updateDeviceLogOptions(d.session_id, { export_asm_logs: e.target.checked })
                            }
                          />
                          ASM
                        </label>
                      )}
                      {d.prov_afm && (
                        <label className="check">
                          <input
                            type="checkbox"
                            checked={d.export_afm_logs !== false}
                            onChange={(e) =>
                              void updateDeviceLogOptions(d.session_id, { export_afm_logs: e.target.checked })
                            }
                          />
                          AFM
                        </label>
                      )}
                      {d.prov_avr && (
                        <label className="check">
                          <input
                            type="checkbox"
                            checked={d.export_avr_logs !== false}
                            onChange={(e) =>
                              void updateDeviceLogOptions(d.session_id, { export_avr_logs: e.target.checked })
                            }
                          />
                          AVR
                        </label>
                      )}
                    </div>
                  )}
                  <div className="device-list-options-row">
                    <span className="muted">System</span>
                    <label className="check" title="Forward BIG-IP system syslog to collector :5140">
                      <input
                        type="checkbox"
                        checked={Boolean(d.export_system_logs)}
                        onChange={(e) =>
                          void updateDeviceLogOptions(d.session_id, { export_system_logs: e.target.checked })
                        }
                      />
                      syslog (:5140)
                    </label>
                  </div>
                </div>
                )}
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
            Export logs (remote syslog/HSL profiles)
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
          Configure{" "}
          <a href={contribRepoUrl} target="_blank" rel="noreferrer">
            OpenTelemetry Collector Contrib
          </a>{" "}
          exporters per pipeline. Sections shown match export modes for your selected BIG-IPs (
          {collectorExportModes.metrics && collectorExportModes.logs
            ? "metrics + logs"
            : collectorExportModes.metrics
              ? "metrics only"
              : collectorExportModes.logs
                ? "logs only"
                : "none"}
          ). Applying config writes <code>otel-collector/generated-config.yaml</code> and
          automatically restarts the collector when docker or kubectl is available.
        </p>
        {collectorActionStatus && (
          <p className="muted">{collectorActionStatus}</p>
        )}
        {collectorExportModes.metrics &&
          renderExporterSection(
            "metrics",
            "Metric exporters",
            "Forward polled BIG-IP metrics (OTLP → collector). A Prometheus exporter on port 8889 is always included for local validation.",
            metricExporters,
            setMetricExporters,
          )}
        {collectorExportModes.logs &&
          renderExporterSection(
            "logs",
            "Log exporters",
            "Forward BIG-IP remote logs received on syslog :5140 and HSL tcplog :5141.",
            logExporters,
            setLogExporters,
          )}
        {!collectorExportModes.metrics && !collectorExportModes.logs && (
          <p className="muted">
            Connect to a BIG-IP with Export metrics and/or Export logs enabled to configure
            collector pipelines.
          </p>
        )}
        <div className="actions">
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
    </div>
  );
}
