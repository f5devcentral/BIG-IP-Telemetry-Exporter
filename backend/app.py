"""FastAPI service: BIG-IP polling, OTLP export, collector configuration."""

from __future__ import annotations

import csv
import logging
import os
import secrets
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

from backend.bigip_client import BigIPClient, BigIPError
from backend.as3_log_profiles import ensure_log_profiles_via_as3
from backend.collector_config import (
    CONTRIB_EXPORTERS_REPO,
    EXPORTER_TYPES,
    GENERATED_CONFIG_PATH,
    build_collector_config,
    list_contrib_components,
    list_exporter_catalog,
    write_collector_config,
)
from backend.metrics_extractor import extract_metrics
from backend.otel_export import MetricsExportLoop, OTLPMetricsPusher
from backend.prometheus_ops import control_status, reload_prometheus, restart_prometheus

REPO_ROOT = Path(__file__).resolve().parent.parent
APIS_CSV = REPO_ROOT / "data" / "bigip_apis.csv"
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"
DEFAULT_OTLP_ENDPOINT = os.environ.get(
    "OTLP_HTTP_ENDPOINT",
    "http://127.0.0.1:4318",
)
SESSION_TTL_SEC = 45 * 60


def _browser_host(request: Request) -> str:
    """Host clients use in the browser (LAN IP, Ingress host, etc.)."""
    if explicit := os.environ.get("ACCESS_HOST", "").strip():
        return explicit.split(":")[0]
    host_header = request.headers.get("host", "127.0.0.1:8001")
    return host_header.split(":")[0]


def _browser_urls(request: Request) -> dict[str, str]:
    host = _browser_host(request)
    prom_port = os.environ.get("PROMETHEUS_BROWSER_PORT", "9090")
    coll_port = os.environ.get("COLLECTOR_METRICS_BROWSER_PORT", "8889")
    return {
        "prometheus_ui": os.environ.get("PROMETHEUS_UI_URL")
        or f"http://{host}:{prom_port}",
        "collector_metrics": os.environ.get("COLLECTOR_METRICS_URL")
        or f"http://{host}:{coll_port}/metrics",
    }


@dataclass
class _Session:
    client: BigIPClient
    host: str
    label: str
    created: float
    token_timeout_sec: int = 1200
    warning: str | None = None
    request_log_profile: str | None = None
    request_log_profile_created: bool | None = None
    asm_log_profile: str | None = None
    asm_log_profile_created: bool | None = None
    afm_log_profile: str | None = None
    afm_log_profile_created: bool | None = None
    http_analytics_profile: str | None = None
    http_analytics_profile_created: bool | None = None
    tcp_analytics_profile: str | None = None
    tcp_analytics_profile_created: bool | None = None
    export_metrics: bool = True
    export_logs: bool = True


_sessions: dict[str, _Session] = {}
_export_loop: MetricsExportLoop | None = None
_pusher: OTLPMetricsPusher | None = None
_collector_exporters: list[dict[str, Any]] = [
    {"type": "prometheus", "enabled": True, "params": {"endpoint": "0.0.0.0:8889"}},
]


def _gc_sessions() -> None:
    now = time.time()
    for sid, s in list(_sessions.items()):
        if now - s.created > SESSION_TTL_SEC:
            try:
                s.client.logout()
            except Exception:
                pass
            _sessions.pop(sid, None)


def _normalize_host(host: str) -> str:
    h = host.strip().rstrip("/")
    if not h.startswith("http://") and not h.startswith("https://"):
        h = f"https://{h}"
    return h


def _display_host(host: str) -> str:
    return host.replace("https://", "").replace("http://", "").split("/")[0]


def _find_session_id_for_host(host: str) -> str | None:
    target = _normalize_host(host)
    for sid, sess in _sessions.items():
        if _normalize_host(sess.host) == target:
            return sid
    return None


def _get_session(session_id: str) -> _Session:
    _gc_sessions()
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Unknown or expired session")
    return s


def _append_warning(warning: str | None, message: str) -> str:
    if warning:
        return f"{warning} {message}"
    return message


def _session_to_dict(session_id: str, sess: _Session) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "host": sess.host,
        "display_host": _display_host(sess.host),
        "label": sess.label,
        "token_timeout_sec": sess.token_timeout_sec,
        "warning": sess.warning,
        "request_log_profile": sess.request_log_profile,
        "request_log_profile_created": sess.request_log_profile_created,
        "asm_log_profile": sess.asm_log_profile,
        "asm_log_profile_created": sess.asm_log_profile_created,
        "afm_log_profile": sess.afm_log_profile,
        "afm_log_profile_created": sess.afm_log_profile_created,
        "http_analytics_profile": sess.http_analytics_profile,
        "http_analytics_profile_created": sess.http_analytics_profile_created,
        "tcp_analytics_profile": sess.tcp_analytics_profile,
        "tcp_analytics_profile_created": sess.tcp_analytics_profile_created,
        "export_metrics": sess.export_metrics,
        "export_logs": sess.export_logs,
        "connected_since": sess.created,
    }


def _normalize_api_row(row: dict[str, str]) -> dict[str, str]:
    """Normalize module/category for paths that share a URL prefix but map to a licensed module."""
    endpoint = (row.get("endpoint") or "").strip().lower()
    out = dict(row)
    if "/mgmt/tm/asm" in endpoint:
        out["module"] = "ASM"
        if not (out.get("category") or "").strip():
            out["category"] = "ASM Module"
        return out
    if "/mgmt/tm/security/firewall" in endpoint:
        out["module"] = "AFM"
        if not (out.get("category") or "").strip():
            out["category"] = "AFM Module"
        return out
    return row


def _load_apis() -> list[dict[str, str]]:
    if not APIS_CSV.exists():
        return []
    with APIS_CSV.open(newline="", encoding="utf-8") as f:
        return [_normalize_api_row(r) for r in csv.DictReader(f)]


def _module_counts(rows: list[dict[str, str]] | None = None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows if rows is not None else _load_apis():
        mod = (row.get("module") or "").strip().upper()
        if mod:
            counts[mod] = counts.get(mod, 0) + 1
    return dict(sorted(counts.items()))


class ConnectBody(BaseModel):
    host: str
    username: str
    password: str
    verify_tls: bool = False
    label: str = ""
    export_metrics: bool = True
    export_logs: bool = True


class ConnectResponse(BaseModel):
    session_id: str
    host: str
    token_timeout_sec: int = 1200
    warning: str | None = None
    request_log_profile: str | None = None
    request_log_profile_created: bool | None = None
    asm_log_profile: str | None = None
    asm_log_profile_created: bool | None = None
    afm_log_profile: str | None = None
    afm_log_profile_created: bool | None = None
    http_analytics_profile: str | None = None
    http_analytics_profile_created: bool | None = None
    tcp_analytics_profile: str | None = None
    tcp_analytics_profile_created: bool | None = None
    export_metrics: bool = True
    export_logs: bool = True


class ExporterItem(BaseModel):
    type: str
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class CollectorConfigBody(BaseModel):
    exporters: list[ExporterItem]
    restart_hint: bool = Field(
        default=True,
        description="When true, response includes docker compose restart command",
    )


class ExportStartBody(BaseModel):
    session_ids: list[str] = Field(
        default_factory=list,
        description="BIG-IP session IDs to poll; empty = all connected devices",
    )
    endpoints: list[str] = Field(default_factory=list)
    metrics_only: bool = True
    modules: list[str] = Field(default_factory=list)
    poll_interval_sec: float = 30.0
    otlp_endpoint: str = Field(
        default_factory=lambda: DEFAULT_OTLP_ENDPOINT,
        description="OTLP HTTP base URL for the collector (backend pushes here)",
    )


class ProbeBody(BaseModel):
    endpoint: str
    session_id: str = ""


app = FastAPI(title="BIG-IP Metrics Exporter", version="1.0.0")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Unexpected errors only — do not swallow HTTPException (would turn 400 into 500)."""
    if isinstance(exc, (HTTPException, StarletteHTTPException)):
        raise exc
    logger.exception("Unhandled error on %s", request.url.path)
    if request.url.path.startswith("/api"):
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc), "type": type(exc).__name__},
        )
    raise exc


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/runtime-config")
def runtime_config(request: Request) -> dict[str, str]:
    """Defaults for the UI; browser URLs follow the request Host (or ACCESS_HOST)."""
    urls = _browser_urls(request)
    return {
        "otlp_endpoint": DEFAULT_OTLP_ENDPOINT,
        "prometheus_ui": urls["prometheus_ui"],
        "collector_metrics": urls["collector_metrics"],
        "collector_config_path": str(GENERATED_CONFIG_PATH),
        "access_host": _browser_host(request),
    }


@app.get("/api/apis")
def list_apis(
    metrics_only: bool = False,
    module: str | None = None,
) -> dict[str, Any]:
    rows = _load_apis()
    if metrics_only:
        rows = [r for r in rows if r.get("collect_metrics", "").lower() == "true"]
    if module:
        mod = module.upper()
        rows = [r for r in rows if (r.get("module") or "").upper() == mod]
    counts = _module_counts()
    return {
        "apis": rows,
        "count": len(rows),
        "modules": list(counts.keys()),
        "module_counts": counts,
    }


@app.get("/api/bigips")
def list_bigips() -> dict[str, Any]:
    _gc_sessions()
    devices = [_session_to_dict(sid, s) for sid, s in _sessions.items()]
    return {"devices": devices, "count": len(devices)}


@app.get("/api/devices")
def list_devices() -> dict[str, Any]:
    return list_bigips()


@app.post("/api/connect", response_model=ConnectResponse)
def connect(body: ConnectBody) -> ConnectResponse:
    if not body.export_metrics and not body.export_logs:
        raise HTTPException(
            status_code=400,
            detail="Select at least one of export metrics or export logs.",
        )
    try:
        existing = _find_session_id_for_host(body.host)
        if existing:
            old = _sessions.pop(existing)
            try:
                old.client.logout()
            except Exception:
                pass

        client = BigIPClient(
            body.host,
            body.username,
            body.password,
            verify_tls=body.verify_tls,
        )
        try:
            client.login()
        except BigIPError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        warning: str | None = None
        token_timeout_sec = 1200
        try:
            client.extend_token()
            token_timeout_sec = 3600
        except BigIPError as exc:
            warning = _append_warning(
                None,
                f"Connected, but could not extend auth token ({exc}). "
                "Long export runs may need to reconnect if the session expires.",
            )

        request_log_profile: str | None = None
        request_log_profile_created: bool | None = None
        asm_log_profile: str | None = None
        asm_log_profile_created: bool | None = None
        afm_log_profile: str | None = None
        afm_log_profile_created: bool | None = None
        http_analytics_profile: str | None = None
        http_analytics_profile_created: bool | None = None
        tcp_analytics_profile: str | None = None
        tcp_analytics_profile_created: bool | None = None
        if body.export_logs:
            try:
                profiles = ensure_log_profiles_via_as3(client)
                request_log_profile = profiles.request_log_profile
                request_log_profile_created = profiles.request_log_profile_created
                asm_log_profile = profiles.asm_log_profile
                asm_log_profile_created = profiles.asm_log_profile_created
                afm_log_profile = profiles.afm_log_profile
                afm_log_profile_created = profiles.afm_log_profile_created
                http_analytics_profile = profiles.http_analytics_profile
                http_analytics_profile_created = profiles.http_analytics_profile_created
                tcp_analytics_profile = profiles.tcp_analytics_profile
                tcp_analytics_profile_created = profiles.tcp_analytics_profile_created
            except BigIPError as exc:
                warning = _append_warning(
                    warning,
                    f"Connected, but could not create or update logging/analytics profiles "
                    f"via AS3 ({exc}).",
                )

        host_norm = _normalize_host(body.host)
        label = (body.label or "").strip() or _display_host(host_norm)
        sid = secrets.token_urlsafe(24)
        _sessions[sid] = _Session(
            client=client,
            host=host_norm,
            label=label,
            created=time.time(),
            token_timeout_sec=token_timeout_sec,
            warning=warning,
            request_log_profile=request_log_profile,
            request_log_profile_created=request_log_profile_created,
            asm_log_profile=asm_log_profile,
            asm_log_profile_created=asm_log_profile_created,
            afm_log_profile=afm_log_profile,
            afm_log_profile_created=afm_log_profile_created,
            http_analytics_profile=http_analytics_profile,
            http_analytics_profile_created=http_analytics_profile_created,
            tcp_analytics_profile=tcp_analytics_profile,
            tcp_analytics_profile_created=tcp_analytics_profile_created,
            export_metrics=body.export_metrics,
            export_logs=body.export_logs,
        )
        return ConnectResponse(
            session_id=sid,
            host=host_norm,
            token_timeout_sec=token_timeout_sec,
            warning=warning,
            request_log_profile=request_log_profile,
            request_log_profile_created=request_log_profile_created,
            asm_log_profile=asm_log_profile,
            asm_log_profile_created=asm_log_profile_created,
            afm_log_profile=afm_log_profile,
            afm_log_profile_created=afm_log_profile_created,
            http_analytics_profile=http_analytics_profile,
            http_analytics_profile_created=http_analytics_profile_created,
            tcp_analytics_profile=tcp_analytics_profile,
            tcp_analytics_profile_created=tcp_analytics_profile_created,
            export_metrics=body.export_metrics,
            export_logs=body.export_logs,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("connect failed for host %s: %s", body.host, traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Connect failed: {exc}",
        ) from exc


@app.delete("/api/session/{session_id}")
def disconnect(session_id: str) -> dict[str, bool]:
    s = _sessions.pop(session_id, None)
    if s:
        try:
            s.client.logout()
        except Exception:
            pass
    return {"ok": True}


@app.post("/api/probe")
def probe_endpoint(
    body: ProbeBody,
    session_id: str = Query(""),
) -> dict[str, Any]:
    sid = body.session_id or session_id
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required")
    s = _get_session(sid)
    try:
        payload = s.client.get(body.endpoint)
        points = extract_metrics(
            body.endpoint, payload, bigip_host=_display_host(s.host)
        )
        return {
            "endpoint": body.endpoint,
            "host": s.host,
            "ok": True,
            "metric_points": len(points),
            "sample": points[:5],
        }
    except BigIPError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/exporters/catalog")
def exporters_catalog() -> dict[str, Any]:
    catalog = list_exporter_catalog()
    categories = sorted({e["category"] for e in catalog})
    return {
        "exporters": catalog,
        "categories": categories,
        "types": list(EXPORTER_TYPES),
        "contrib_components": list_contrib_components(),
        "contrib_repo_url": CONTRIB_EXPORTERS_REPO,
    }


@app.get("/api/collector/config")
def get_collector_config() -> dict[str, Any]:
    cfg = build_collector_config(_collector_exporters)
    return {
        "exporters": _collector_exporters,
        "yaml": _yaml_dump(cfg),
        "generated_path": str(GENERATED_CONFIG_PATH),
    }


@app.post("/api/collector/config")
def apply_collector_config(body: CollectorConfigBody) -> dict[str, Any]:
    global _collector_exporters
    _collector_exporters = [e.model_dump() for e in body.exporters]
    try:
        path = write_collector_config(_collector_exporters)
        cfg = build_collector_config(_collector_exporters)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    restart = os.environ.get(
        "COLLECTOR_RESTART_HINT",
        "docker compose restart otel-collector",
    )
    return {
        "ok": True,
        "path": str(path),
        "yaml": _yaml_dump(cfg),
        "restart_command": restart,
        "k8s_apply_hint": (
            "kubectl -n bigip-metrics create configmap otel-collector-config "
            "--from-file=config.yaml=<path> --dry-run=client -o yaml | kubectl apply -f - "
            "&& kubectl -n bigip-metrics rollout restart deployment/otel-collector"
        ),
    }


@app.get("/api/export/status")
def export_status() -> dict[str, Any]:
    global _export_loop, _pusher
    _gc_sessions()
    return {
        "loop": _export_loop.status if _export_loop else {"running": False},
        "otlp_endpoint": getattr(_pusher, "_endpoint", None) if _pusher else None,
        "connected_devices": [
            _session_to_dict(sid, s) for sid, s in _sessions.items()
        ],
    }


def _resolve_export_clients(session_ids: list[str]) -> list[tuple[str, str, BigIPClient]]:
    _gc_sessions()
    ids = session_ids or list(_sessions.keys())
    if not ids:
        raise HTTPException(
            status_code=400,
            detail="No BIG-IP devices connected. Add at least one device before starting export.",
        )
    clients: list[tuple[str, str, BigIPClient]] = []
    skipped: list[str] = []
    for sid in ids:
        sess = _get_session(sid)
        if not sess.export_metrics:
            skipped.append(sess.label or _display_host(sess.host))
            continue
        clients.append((_display_host(sess.host), sid, sess.client))
    if not clients:
        detail = "No selected devices have metrics export enabled."
        if skipped:
            detail += f" Skipped (logs only): {', '.join(skipped)}."
        raise HTTPException(status_code=400, detail=detail)
    return clients


@app.post("/api/export/start")
def export_start(body: ExportStartBody) -> dict[str, Any]:
    global _export_loop, _pusher
    clients = _resolve_export_clients(body.session_ids)
    endpoints = body.endpoints
    if not endpoints:
        rows = _load_apis()
        if body.metrics_only:
            rows = [r for r in rows if r.get("collect_metrics", "").lower() == "true"]
        if body.modules:
            mods = {m.upper() for m in body.modules}
            rows = [r for r in rows if (r.get("module") or "").upper() in mods]
        endpoints = [r["endpoint"] for r in rows]
    if not endpoints:
        raise HTTPException(status_code=400, detail="No endpoints selected")

    if _export_loop and _export_loop.status.get("running"):
        _export_loop.stop()
    if _pusher:
        _pusher.shutdown()

    otlp = body.otlp_endpoint.rstrip("/")
    _pusher = OTLPMetricsPusher(otlp)
    _export_loop = MetricsExportLoop(
        clients,
        endpoints,
        _pusher,
        poll_interval_sec=body.poll_interval_sec,
    )
    result = _export_loop.run_once()
    _export_loop.start_background()
    return {
        "started": True,
        "endpoints": len(endpoints),
        "bigip_count": len(clients),
        "bigip_hosts": [h for h, _, _ in clients],
        "first_run": result,
        "status": _export_loop.status,
    }


@app.post("/api/export/stop")
def export_stop() -> dict[str, Any]:
    global _export_loop, _pusher
    if _export_loop:
        _export_loop.stop()
    if _pusher:
        _pusher.shutdown()
    _export_loop = None
    _pusher = None
    return {"stopped": True}


@app.get("/api/validation/prometheus")
def prometheus_hints(request: Request) -> dict[str, str]:
    urls = _browser_urls(request)
    return {
        "prometheus_ui": urls["prometheus_ui"],
        "collector_metrics": urls["collector_metrics"],
        "query_example": "bigip_tm_ltm_virtual_stats",
        "scrape_job": "otel-collector",
    }


@app.get("/api/prometheus/control")
def prometheus_control(request: Request) -> dict[str, Any]:
    return control_status(request_host=_browser_host(request))


@app.post("/api/prometheus/reload")
def prometheus_reload(request: Request) -> dict[str, Any]:
    try:
        return reload_prometheus(request_host=_browser_host(request))
    except BigIPError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/prometheus/restart")
def prometheus_restart() -> dict[str, Any]:
    try:
        return restart_prometheus()
    except BigIPError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _yaml_dump(cfg: dict[str, Any]) -> str:
    import yaml

    return yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False)


def _ui_setup_hint() -> str:
    return (
        "<!DOCTYPE html><html><head><title>BIG-IP Metrics Exporter</title></head><body>"
        "<h1>BIG-IP Metrics Exporter</h1>"
        "<p>The web UI is not built yet. From the project root run:</p>"
        "<pre>cd frontend && npm ci && npm run build</pre>"
        "<p>Then restart <code>python run_server.py</code> and open "
        "<code>http://&lt;HOST-IP&gt;:8001</code>.</p>"
        "<p>API is available: <a href=\"/docs\">/docs</a> · "
        '<a href="/api/health">/api/health</a></p>'
        "</body></html>"
    )


def _register_ui_routes() -> None:
    """Serve the React build without mounting StaticFiles at '/' (avoids API route conflicts)."""
    index_html = FRONTEND_DIST / "index.html"
    assets_dir = FRONTEND_DIST / "assets"

    @app.get("/", include_in_schema=False, response_model=None)
    def ui_index() -> FileResponse | HTMLResponse:
        if index_html.is_file():
            return FileResponse(index_html)
        return HTMLResponse(_ui_setup_hint(), status_code=200)

    if assets_dir.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=str(assets_dir)),
            name="ui-assets",
        )

    @app.get("/{ui_path:path}", include_in_schema=False, response_model=None)
    def ui_fallback(ui_path: str) -> FileResponse | HTMLResponse:
        if ui_path.startswith("api/") or ui_path == "api":
            raise HTTPException(status_code=404, detail="Not Found")
        if ui_path.startswith("assets/"):
            raise HTTPException(status_code=404, detail="Not Found")
        if index_html.is_file():
            return FileResponse(index_html)
        return HTMLResponse(_ui_setup_hint(), status_code=200)


_register_ui_routes()
