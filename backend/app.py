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
from backend.collector_config import (
    EXPORTER_TYPES,
    GENERATED_CONFIG_PATH,
    build_collector_config,
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
    created: float


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


def _get_session(session_id: str) -> _Session:
    _gc_sessions()
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Unknown or expired session")
    return s


def _load_apis() -> list[dict[str, str]]:
    if not APIS_CSV.exists():
        return []
    with APIS_CSV.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class ConnectBody(BaseModel):
    host: str
    username: str
    password: str
    verify_tls: bool = False


class ConnectResponse(BaseModel):
    session_id: str
    host: str
    token_timeout_sec: int = 1200
    warning: str | None = None


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
    modules = sorted({(r.get("module") or "").upper() for r in _load_apis() if r.get("module")})
    return {"apis": rows, "count": len(rows), "modules": modules}


@app.post("/api/connect", response_model=ConnectResponse)
def connect(body: ConnectBody) -> ConnectResponse:
    try:
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
            warning = (
                f"Connected, but could not extend auth token ({exc}). "
                "Long export runs may need to reconnect if the session expires."
            )

        sid = secrets.token_urlsafe(24)
        _sessions[sid] = _Session(client=client, created=time.time())
        return ConnectResponse(
            session_id=sid,
            host=body.host,
            token_timeout_sec=token_timeout_sec,
            warning=warning,
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
def probe_endpoint(body: ProbeBody, session_id: str = Query(...)) -> dict[str, Any]:
    s = _get_session(session_id)
    try:
        payload = s.client.get(body.endpoint)
        points = extract_metrics(body.endpoint, payload)
        return {
            "endpoint": body.endpoint,
            "ok": True,
            "metric_points": len(points),
            "sample": points[:5],
        }
    except BigIPError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/exporters/catalog")
def exporters_catalog() -> dict[str, Any]:
    return {"exporters": list_exporter_catalog(), "types": list(EXPORTER_TYPES)}


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
    path = write_collector_config(_collector_exporters)
    cfg = build_collector_config(_collector_exporters)
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
    return {
        "loop": _export_loop.status if _export_loop else {"running": False},
        "otlp_endpoint": getattr(_pusher, "_endpoint", None) if _pusher else None,
    }


@app.post("/api/export/start")
def export_start(body: ExportStartBody, session_id: str = Query(...)) -> dict[str, Any]:
    global _export_loop, _pusher
    s = _get_session(session_id)
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
        s.client,
        endpoints,
        _pusher,
        poll_interval_sec=body.poll_interval_sec,
    )
    result = _export_loop.run_once()
    _export_loop.start_background()
    return {
        "started": True,
        "endpoints": len(endpoints),
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
