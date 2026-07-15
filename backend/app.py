"""FastAPI service: BIG-IP polling, OTLP export, collector configuration."""

from __future__ import annotations

import csv
import logging
import os
import secrets
import time
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
from backend.module_provision import is_module_provisioned
from backend.collector_config import (
    CONTRIB_EXPORTERS_REPO,
    EXPORTER_TYPES,
    GENERATED_CONFIG_PATH,
    build_collector_config,
    exporter_selections_from_config,
    list_contrib_components,
    list_exporter_catalog,
    read_collector_config_file,
    write_collector_config,
)
from backend.collector_ops import (
    auto_restart_enabled,
    control_status as collector_control_status,
    probe_prometheus_scrape,
    restart_collector,
    restart_hint,
    restart_prometheus,
)
from backend.log_forwarding import resolve_syslog_host, runtime_log_config
from backend.log_rollback import (
    clear_session_log_resources,
    rollback_log_resources,
    session_has_log_resources,
)
from backend.metrics_extractor import extract_metrics
from backend.otel_export import MetricsExportLoop, OTLPMetricsPusher
from backend.session_store import (
    clear_store,
    load_store,
    persist_enabled,
    save_store,
    session_record_from_dict,
    store_path,
    stored_credentials,
)
from backend.system_syslog import ensure_system_syslog_forwarding

REPO_ROOT = Path(__file__).resolve().parent.parent
APIS_CSV = REPO_ROOT / "data" / "bigip_apis.csv"
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"
FAVICON_SVG = REPO_ROOT / "F5-logo-F5-rgb.svg"
DEFAULT_OTLP_ENDPOINT = os.environ.get(
    "OTLP_HTTP_ENDPOINT",
    "http://127.0.0.1:4318",
)
_LOCAL_OTLP_ENDPOINTS = frozenset(
    {
        "http://127.0.0.1:4318",
        "http://localhost:4318",
    }
)
SESSION_TTL_SEC = int(os.environ.get("BIGIP_SESSION_TTL_SEC", str(45 * 60)))


def _resolve_otlp_endpoint(requested: str | None) -> str:
    """Prefer server OTLP_HTTP_ENDPOINT when the UI still sends localhost defaults."""
    server = DEFAULT_OTLP_ENDPOINT.rstrip("/")
    req = (requested or "").strip().rstrip("/")
    if not req:
        return server
    if req in _LOCAL_OTLP_ENDPOINTS and server not in _LOCAL_OTLP_ENDPOINTS:
        return server
    return req


def _browser_host(request: Request) -> str:
    """Host clients use in the browser (LAN IP, Ingress host, etc.)."""
    if explicit := os.environ.get("ACCESS_HOST", "").strip():
        return explicit.split(":")[0]
    host_header = request.headers.get("host", "127.0.0.1:8001")
    return host_header.split(":")[0]


@dataclass
class _Session:
    client: BigIPClient
    host: str
    label: str
    created: float
    username: str = ""
    password: str = ""
    verify_tls: bool = False
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
    log_syslog_target: str | None = None
    log_hsl_target: str | None = None
    system_syslog_target: str | None = None
    export_metrics: bool = True
    export_logs: bool = True  # derived compatibility flag
    export_system_logs: bool = False
    export_ltm_logs: bool = True
    export_asm_logs: bool = True
    export_afm_logs: bool = True
    export_avr_logs: bool = True
    prov_ltm: bool = True
    prov_asm: bool = False
    prov_afm: bool = False
    prov_avr: bool = False
    metric_endpoints: list[str] = field(default_factory=list)


_sessions: dict[str, _Session] = {}
_export_loop: MetricsExportLoop | None = None
_pusher: OTLPMetricsPusher | None = None
_log_export: dict[str, Any] = {"active": False, "hosts": [], "syslog_target": None, "hsl_target": None}
_collector_metric_exporters: list[dict[str, Any]] = []
_collector_log_exporters: list[dict[str, Any]] = []
_collector_export_metrics: bool = True
_collector_export_logs: bool = True
_export_config: dict[str, Any] | None = None


def _session_persist_payload(session_id: str, sess: _Session) -> dict[str, Any]:
    return session_record_from_dict(
        session_id,
        {
            "host": sess.host,
            "username": sess.username,
            "password": sess.password,
            "verify_tls": sess.verify_tls,
            "label": sess.label,
            "created": sess.created,
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
            "log_syslog_target": sess.log_syslog_target,
            "log_hsl_target": sess.log_hsl_target,
            "system_syslog_target": sess.system_syslog_target,
            "export_metrics": sess.export_metrics,
            "export_logs": sess.export_logs,
            "export_system_logs": sess.export_system_logs,
            "export_ltm_logs": sess.export_ltm_logs,
            "export_asm_logs": sess.export_asm_logs,
            "export_afm_logs": sess.export_afm_logs,
            "export_avr_logs": sess.export_avr_logs,
            "prov_ltm": sess.prov_ltm,
            "prov_asm": sess.prov_asm,
            "prov_afm": sess.prov_afm,
            "prov_avr": sess.prov_avr,
            "metric_endpoints": list(sess.metric_endpoints),
        },
    )


def _persist_runtime_state() -> None:
    if not persist_enabled():
        return
    records = [
        _session_persist_payload(sid, sess) for sid, sess in _sessions.items()
    ]
    save_store(records, _export_config)


def _gc_sessions() -> None:
    if persist_enabled():
        return
    now = time.time()
    for sid, s in list(_sessions.items()):
        if now - s.created > SESSION_TTL_SEC:
            try:
                s.client.logout()
            except Exception:
                pass
            _sessions.pop(sid, None)
    _persist_runtime_state()


def _login_client(host: str, username: str, password: str, *, verify_tls: bool) -> tuple[BigIPClient, int, str | None]:
    client = BigIPClient(host, username, password, verify_tls=verify_tls)
    client.login()
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
    return client, token_timeout_sec, warning


def _session_from_record(record: dict[str, Any]) -> _Session:
    host, username, password, verify_tls = stored_credentials(record)
    client, token_timeout_sec, login_warning = _login_client(
        host,
        username,
        password,
        verify_tls=verify_tls,
    )
    warning = record.get("warning")
    if login_warning:
        warning = _append_warning(
            str(warning) if warning else None,
            login_warning,
        )
    label = (record.get("label") or "").strip() or _display_host(host)
    return _Session(
        client=client,
        host=_normalize_host(host),
        label=label,
        created=float(record.get("created") or time.time()),
        username=username,
        password=password,
        verify_tls=verify_tls,
        token_timeout_sec=token_timeout_sec,
        warning=warning if isinstance(warning, str) else None,
        request_log_profile=record.get("request_log_profile"),
        request_log_profile_created=record.get("request_log_profile_created"),
        asm_log_profile=record.get("asm_log_profile"),
        asm_log_profile_created=record.get("asm_log_profile_created"),
        afm_log_profile=record.get("afm_log_profile"),
        afm_log_profile_created=record.get("afm_log_profile_created"),
        http_analytics_profile=record.get("http_analytics_profile"),
        http_analytics_profile_created=record.get("http_analytics_profile_created"),
        tcp_analytics_profile=record.get("tcp_analytics_profile"),
        tcp_analytics_profile_created=record.get("tcp_analytics_profile_created"),
        log_syslog_target=record.get("log_syslog_target"),
        log_hsl_target=record.get("log_hsl_target"),
        system_syslog_target=record.get("system_syslog_target"),
        export_metrics=bool(record.get("export_metrics", True)),
        export_logs=bool(record.get("export_logs", True)),
        export_system_logs=bool(record.get("export_system_logs", False)),
        export_ltm_logs=bool(record.get("export_ltm_logs", True)),
        export_asm_logs=bool(record.get("export_asm_logs", True)),
        export_afm_logs=bool(record.get("export_afm_logs", True)),
        export_avr_logs=bool(record.get("export_avr_logs", True)),
        prov_ltm=bool(record.get("prov_ltm", True)),
        prov_asm=bool(record.get("prov_asm", False)),
        prov_afm=bool(record.get("prov_afm", False)),
        prov_avr=bool(record.get("prov_avr", False)),
        metric_endpoints=[
            str(ep) for ep in (record.get("metric_endpoints") or []) if str(ep).strip()
        ],
    )


def _hydrate_collector_from_disk() -> None:
    global _collector_metric_exporters, _collector_log_exporters
    cfg = read_collector_config_file()
    if not cfg:
        return
    metrics, logs = exporter_selections_from_config(cfg)
    if metrics:
        _collector_metric_exporters = metrics
    if logs:
        _collector_log_exporters = logs


def _restore_runtime_state() -> None:
    global _export_config, _export_loop, _pusher, _log_export
    _hydrate_collector_from_disk()
    records, export_state = load_store()
    if not records and not export_state:
        return
    restored = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        session_id = str(record.get("session_id") or "").strip()
        if not session_id:
            continue
        try:
            _sessions[session_id] = _session_from_record(record)
            restored += 1
        except (BigIPError, ValueError) as exc:
            logger.warning(
                "Could not restore BIG-IP session %s (%s): %s",
                session_id,
                record.get("host"),
                exc,
            )
    if restored:
        logger.info("Restored %d BIG-IP session(s) from %s", restored, store_path())
    if export_state and export_state.get("active"):
        _export_config = export_state
        session_ids = [
            sid for sid in export_state.get("session_ids") or [] if sid in _sessions
        ]
        if not session_ids and _sessions:
            session_ids = list(_sessions.keys())
        if session_ids:
            try:
                _start_export(
                    ExportStartBody(
                        session_ids=session_ids,
                        endpoints=list(export_state.get("endpoints") or []),
                        metrics_only=bool(export_state.get("metrics_only", True)),
                        modules=list(export_state.get("modules") or []),
                        poll_interval_sec=float(export_state.get("poll_interval_sec", 30)),
                        otlp_endpoint=str(
                            export_state.get("otlp_endpoint") or DEFAULT_OTLP_ENDPOINT
                        ),
                    )
                )
                logger.info("Resumed metrics/log export from persisted state")
            except HTTPException as exc:
                logger.warning("Could not resume export: %s", exc.detail)
        else:
            _export_config = {**export_state, "active": False}
            _persist_runtime_state()


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
        "log_syslog_target": sess.log_syslog_target,
        "log_hsl_target": sess.log_hsl_target,
        "system_syslog_target": sess.system_syslog_target,
        "export_metrics": sess.export_metrics,
        "export_logs": sess.export_logs,
        "export_system_logs": sess.export_system_logs,
        "export_ltm_logs": sess.export_ltm_logs,
        "export_asm_logs": sess.export_asm_logs,
        "export_afm_logs": sess.export_afm_logs,
        "export_avr_logs": sess.export_avr_logs,
        "prov_ltm": sess.prov_ltm,
        "prov_asm": sess.prov_asm,
        "prov_afm": sess.prov_afm,
        "prov_avr": sess.prov_avr,
        "connected_since": sess.created,
        "has_log_resources": session_has_log_resources(sess),
        "metric_endpoints": list(sess.metric_endpoints),
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


def _resolve_metric_endpoints_for_session(sess: _Session) -> list[str]:
    """Return the session's explicit metric endpoint selection (empty means none)."""
    return list(sess.metric_endpoints)


def _live_metric_endpoints_for_session(session_id: str) -> list[str]:
    """Resolve the current metric endpoints for a session (used each export poll)."""
    sess = _sessions.get(session_id)
    if not sess or not sess.export_metrics:
        return []
    return _resolve_metric_endpoints_for_session(sess)


def _apply_metric_endpoints_by_session(endpoints_by_session: dict[str, list[str]]) -> None:
    for sid, eps in endpoints_by_session.items():
        sess = _sessions.get(sid)
        if not sess:
            continue
        sess.metric_endpoints = [ep.strip() for ep in eps if ep.strip()]


def _export_start_body_from_config() -> ExportStartBody | None:
    if not _export_config or not _export_config.get("active"):
        return None
    session_ids = [
        sid for sid in (_export_config.get("session_ids") or []) if sid in _sessions
    ]
    if not session_ids:
        return None
    return ExportStartBody(
        session_ids=session_ids,
        endpoints_by_session=dict(_export_config.get("endpoints_by_session") or {}),
        metrics_only=bool(_export_config.get("metrics_only", True)),
        modules=list(_export_config.get("modules") or []),
        poll_interval_sec=float(_export_config.get("poll_interval_sec", 30)),
        otlp_endpoint=_resolve_otlp_endpoint(
            str(_export_config.get("otlp_endpoint") or DEFAULT_OTLP_ENDPOINT)
        ),
    )


def _restart_active_export() -> None:
    body = _export_start_body_from_config()
    if body:
        _start_export(body)


def _stop_metrics_export(*, join_timeout_sec: float = 10.0) -> None:
    global _export_loop, _pusher
    if _export_loop:
        _export_loop.stop(join_timeout_sec=join_timeout_sec)
    if _pusher:
        _pusher.shutdown()
    _export_loop = None
    _pusher = None


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
    export_logs: bool = True  # deprecated umbrella; use per-type flags
    export_system_logs: bool = False
    export_ltm_logs: bool = True
    export_asm_logs: bool = True
    export_afm_logs: bool = True
    export_avr_logs: bool = True


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
    log_syslog_target: str | None = None
    log_hsl_target: str | None = None
    export_metrics: bool = True
    export_logs: bool = True
    export_system_logs: bool = False
    export_ltm_logs: bool = True
    export_asm_logs: bool = True
    export_afm_logs: bool = True
    export_avr_logs: bool = True
    prov_ltm: bool = True
    prov_asm: bool = False
    prov_afm: bool = False
    prov_avr: bool = False


class ExporterItem(BaseModel):
    type: str
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class CollectorConfigBody(BaseModel):
    metric_exporters: list[ExporterItem] = Field(default_factory=list)
    log_exporters: list[ExporterItem] = Field(default_factory=list)
    exporters: list[ExporterItem] | None = Field(
        default=None,
        description="Deprecated: treated as metric_exporters when metric/log lists are empty",
    )
    export_metrics: bool = True
    export_logs: bool = True
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
    endpoints_by_session: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Per-device metric API paths from the UI; applied before export starts",
    )
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


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    _restore_runtime_state()
    yield


app = FastAPI(title="BIG-IP Telemetry Exporter", version="1.0.0", lifespan=_app_lifespan)


class LogOptionsBody(BaseModel):
    export_system_logs: bool | None = None
    export_ltm_logs: bool | None = None
    export_asm_logs: bool | None = None
    export_afm_logs: bool | None = None
    export_avr_logs: bool | None = None


class MetricEndpointsBody(BaseModel):
    endpoints: list[str] = Field(default_factory=list)


class RollbackBody(BaseModel):
    confirm: bool = Field(
        default=False,
        description="Must be true to execute destructive rollback (safety latch)",
    )
    delete_as3: bool = Field(
        default=True,
        description="DELETE the AS3 application with exporter log profiles and pools",
    )
    remove_system_syslog: bool = Field(
        default=True,
        description="Remove exporter-managed syslog-ng include from /sys/syslog",
    )
    save_sys_config_after: bool = Field(
        default=True,
        description="POST save sys config after rollback changes",
    )


class NukeBody(BaseModel):
    confirm: bool = Field(
        default=False,
        description="Must be true to execute a full application reset (safety latch)",
    )
    rollback_bigip: bool = Field(
        default=True,
        description="Roll back AS3 log profiles and system syslog on each connected BIG-IP",
    )
    reset_collector: bool = Field(
        default=True,
        description="Reset collector exporters to defaults and rewrite generated config",
    )
    restart_collector: bool = Field(
        default=True,
        description="Restart collector after resetting config (when reset_collector is true)",
    )
    restart_prometheus: bool = Field(
        default=True,
        description=(
            "Wipe and recreate local Prometheus so TSDB is empty (fresh install). "
            "Docker Compose force-recreates the container; Kubernetes deletes pods "
            "to discard emptyDir data."
        ),
    )
    remove_encryption_key: bool = Field(
        default=False,
        description="Also delete the local Fernet key file used for session persistence",
    )


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
    return {
        "otlp_endpoint": DEFAULT_OTLP_ENDPOINT,
        "collector_config_path": str(GENERATED_CONFIG_PATH),
        "access_host": _browser_host(request),
        **runtime_log_config(browser_host=_browser_host(request)),
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
def connect(body: ConnectBody, request: Request) -> ConnectResponse:
    any_logs = bool(body.export_metrics or body.export_system_logs or body.export_logs)
    if not any_logs:
        raise HTTPException(
            status_code=400,
            detail="Select at least one of export metrics or export logs.",
        )
    module_logs = bool(body.export_logs)
    export_ltm_logs = body.export_ltm_logs if module_logs else False
    export_asm_logs = body.export_asm_logs if module_logs else False
    export_afm_logs = body.export_afm_logs if module_logs else False
    export_avr_logs = body.export_avr_logs if module_logs else False
    try:
        existing = _find_session_id_for_host(body.host)
        if existing:
            old = _sessions.pop(existing)
            try:
                old.client.logout()
            except Exception:
                pass

        try:
            client, token_timeout_sec, login_warning = _login_client(
                body.host,
                body.username,
                body.password,
                verify_tls=body.verify_tls,
            )
        except BigIPError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        warning: str | None = login_warning

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
        log_syslog_target: str | None = None
        log_hsl_target: str | None = None
        system_syslog_target: str | None = None
        prov_ltm = False
        prov_asm = False
        prov_afm = False
        prov_avr = False
        try:
            prov_ltm = is_module_provisioned(client, "ltm")
            prov_asm = is_module_provisioned(client, "asm")
            prov_afm = is_module_provisioned(client, "afm")
            prov_avr = is_module_provisioned(client, "avr")
        except BigIPError as exc:
            warning = _append_warning(
                warning,
                f"Connected, but could not read module provisioning ({exc}).",
            )
        if body.export_system_logs:
            try:
                log_host = resolve_syslog_host(browser_host=_browser_host(request))
                ensure_system_syslog_forwarding(client, host=log_host, port=5140)
                system_syslog_target = f"{log_host}:5140"
            except ValueError as exc:
                warning = _append_warning(warning, str(exc))
            except BigIPError as exc:
                warning = _append_warning(
                    warning,
                    f"Connected, but could not enable system log forwarding ({exc}).",
                )
        if module_logs and (export_ltm_logs or export_asm_logs or export_afm_logs or export_avr_logs):
            try:
                log_host = resolve_syslog_host(browser_host=_browser_host(request))
                profiles = ensure_log_profiles_via_as3(
                    client,
                    log_host=log_host,
                    include_ltm=export_ltm_logs,
                    include_asm=export_asm_logs,
                    include_afm=export_afm_logs,
                    include_avr=export_avr_logs,
                )
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
                log_syslog_target = profiles.log_syslog_target
                log_hsl_target = profiles.log_hsl_target
            except ValueError as exc:
                warning = _append_warning(warning, str(exc))
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
            username=body.username,
            password=body.password,
            verify_tls=body.verify_tls,
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
            log_syslog_target=log_syslog_target,
            log_hsl_target=log_hsl_target,
            system_syslog_target=system_syslog_target,
            export_metrics=body.export_metrics,
            export_logs=module_logs,
            export_system_logs=body.export_system_logs,
            export_ltm_logs=export_ltm_logs,
            export_asm_logs=export_asm_logs,
            export_afm_logs=export_afm_logs,
            export_avr_logs=export_avr_logs,
            prov_ltm=prov_ltm,
            prov_asm=prov_asm,
            prov_afm=prov_afm,
            prov_avr=prov_avr,
            metric_endpoints=[],
        )
        _persist_runtime_state()
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
            log_syslog_target=log_syslog_target,
            log_hsl_target=log_hsl_target,
            export_metrics=body.export_metrics,
            export_logs=module_logs,
            export_system_logs=body.export_system_logs,
            export_ltm_logs=export_ltm_logs,
            export_asm_logs=export_asm_logs,
            export_afm_logs=export_afm_logs,
            export_avr_logs=export_avr_logs,
            prov_ltm=prov_ltm,
            prov_asm=prov_asm,
            prov_afm=prov_afm,
            prov_avr=prov_avr,
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
    _persist_runtime_state()
    return {"ok": True}


@app.patch("/api/session/{session_id}/log-options")
def update_log_options(session_id: str, body: LogOptionsBody, request: Request) -> dict[str, Any]:
    s = _get_session(session_id)

    # Validate enables against provisioning; always allow disabling.
    def _set(name: str, value: bool | None) -> None:
        if value is None:
            return
        setattr(s, name, bool(value))

    if body.export_ltm_logs is True and not s.prov_ltm:
        raise HTTPException(status_code=400, detail="LTM is not provisioned on this BIG-IP.")
    if body.export_asm_logs is True and not s.prov_asm:
        raise HTTPException(status_code=400, detail="ASM is not provisioned on this BIG-IP.")
    if body.export_afm_logs is True and not s.prov_afm:
        raise HTTPException(status_code=400, detail="AFM is not provisioned on this BIG-IP.")
    if body.export_avr_logs is True and not s.prov_avr:
        raise HTTPException(status_code=400, detail="AVR is not provisioned on this BIG-IP.")

    _set("export_system_logs", body.export_system_logs)
    _set("export_ltm_logs", body.export_ltm_logs)
    _set("export_asm_logs", body.export_asm_logs)
    _set("export_afm_logs", body.export_afm_logs)
    _set("export_avr_logs", body.export_avr_logs)

    warning: str | None = None
    # Apply system syslog forwarding if enabled.
    if s.export_system_logs:
        try:
            log_host = resolve_syslog_host(browser_host=_browser_host(request))
            ensure_system_syslog_forwarding(s.client, host=log_host, port=5140)
            s.system_syslog_target = f"{log_host}:5140"
        except (ValueError, BigIPError) as exc:
            warning = _append_warning(warning, f"System log forwarding update failed ({exc}).")

    # Apply AS3 profiles as requested (only when module logs were enabled at connect).
    if s.export_logs and (
        s.export_ltm_logs or s.export_asm_logs or s.export_afm_logs or s.export_avr_logs
    ):
        try:
            log_host = resolve_syslog_host(browser_host=_browser_host(request))
            profiles = ensure_log_profiles_via_as3(
                s.client,
                log_host=log_host,
                include_ltm=s.export_ltm_logs,
                include_asm=s.export_asm_logs,
                include_afm=s.export_afm_logs,
                include_avr=s.export_avr_logs,
            )
            s.request_log_profile = profiles.request_log_profile
            s.asm_log_profile = profiles.asm_log_profile
            s.afm_log_profile = profiles.afm_log_profile
            s.http_analytics_profile = profiles.http_analytics_profile
            s.tcp_analytics_profile = profiles.tcp_analytics_profile
            s.log_syslog_target = profiles.log_syslog_target
            s.log_hsl_target = profiles.log_hsl_target
        except (ValueError, BigIPError) as exc:
            warning = _append_warning(warning, f"AS3 profile update failed ({exc}).")

    if warning:
        s.warning = _append_warning(s.warning, warning)

    _persist_runtime_state()
    return {"ok": True, "device": _session_to_dict(session_id, s)}


@app.patch("/api/session/{session_id}/metric-endpoints")
def update_metric_endpoints(session_id: str, body: MetricEndpointsBody) -> dict[str, Any]:
    global _export_config
    s = _get_session(session_id)
    s.metric_endpoints = [ep.strip() for ep in body.endpoints if ep.strip()]
    if _export_config and _export_config.get("active"):
        by_session = dict(_export_config.get("endpoints_by_session") or {})
        by_session[session_id] = list(s.metric_endpoints)
        _export_config = {
            **_export_config,
            "endpoints_by_session": by_session,
            "endpoints": sorted({ep for eps in by_session.values() for ep in eps}),
        }
    _persist_runtime_state()
    export_restarted = False
    if _export_loop and _export_loop.status.get("running") and _export_config:
        try:
            _restart_active_export()
            export_restarted = True
        except HTTPException:
            pass
    return {
        "ok": True,
        "device": _session_to_dict(session_id, s),
        "export_restarted": export_restarted,
    }


@app.post("/api/session/{session_id}/rollback")
def rollback_session_log_config(session_id: str, body: RollbackBody) -> dict[str, Any]:
    """Remove exporter-managed AS3 log profiles and system syslog forwarding on a BIG-IP."""
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Rollback refused: set confirm=true in the JSON body (safety latch).",
        )
    s = _get_session(session_id)
    try:
        steps = rollback_log_resources(
            s.client,
            delete_as3=body.delete_as3,
            remove_system_syslog=body.remove_system_syslog,
            save_sys_config_after=body.save_sys_config_after,
        )
    except BigIPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    clear_session_log_resources(s)
    _persist_runtime_state()
    return {
        "ok": True,
        "steps": steps,
        "device": _session_to_dict(session_id, s),
    }


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
    cfg = build_collector_config(
        _collector_metric_exporters,
        _collector_log_exporters,
        export_metrics=_collector_export_metrics,
        export_logs=_collector_export_logs,
    )
    return {
        "metric_exporters": _collector_metric_exporters,
        "log_exporters": _collector_log_exporters,
        "exporters": _collector_metric_exporters,
        "export_metrics": _collector_export_metrics,
        "export_logs": _collector_export_logs,
        "yaml": _yaml_dump(cfg),
        "generated_path": str(GENERATED_CONFIG_PATH),
    }


@app.post("/api/collector/config")
def apply_collector_config(body: CollectorConfigBody) -> dict[str, Any]:
    global _collector_metric_exporters, _collector_log_exporters
    global _collector_export_metrics, _collector_export_logs

    metric_items = body.metric_exporters
    log_items = body.log_exporters
    if body.exporters and not metric_items and not log_items:
        metric_items = body.exporters

    _collector_metric_exporters = [e.model_dump() for e in metric_items]
    _collector_log_exporters = [e.model_dump() for e in log_items]
    _collector_export_metrics = body.export_metrics
    _collector_export_logs = body.export_logs
    try:
        path = write_collector_config(
            _collector_metric_exporters,
            _collector_log_exporters,
            export_metrics=_collector_export_metrics,
            export_logs=_collector_export_logs,
        )
        cfg = build_collector_config(
            _collector_metric_exporters,
            _collector_log_exporters,
            export_metrics=_collector_export_metrics,
            export_logs=_collector_export_logs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    restart_info: dict[str, Any] = {"attempted": False}
    if auto_restart_enabled():
        restart_info["attempted"] = True
        try:
            restart_info.update(restart_collector(config_path=path))
        except BigIPError as exc:
            mode = collector_control_status()["restart_mode"]
            restart_info.update(
                {
                    "ok": False,
                    "error": str(exc),
                    "manual_hint": restart_hint(mode),
                },
            )
    else:
        restart_info["skipped"] = True
        restart_info["manual_hint"] = os.environ.get(
            "COLLECTOR_RESTART_HINT",
            restart_hint(collector_control_status()["restart_mode"]),
        )

    scrape_info: dict[str, Any] | None = None
    if _collector_export_metrics and restart_info.get("ok"):
        scrape_info = probe_prometheus_scrape()

    return {
        "ok": True,
        "path": str(path),
        "yaml": _yaml_dump(cfg),
        "metric_exporters": _collector_metric_exporters,
        "log_exporters": _collector_log_exporters,
        "export_metrics": _collector_export_metrics,
        "export_logs": _collector_export_logs,
        "collector_restart": restart_info,
        "prometheus_scrape": scrape_info,
        "restart_command": restart_info.get("command")
        or restart_info.get("manual_hint")
        or restart_hint(collector_control_status()["restart_mode"]),
        "k8s_apply_hint": restart_hint("kubernetes"),
    }


@app.get("/api/collector/control")
def collector_control() -> dict[str, Any]:
    return collector_control_status()


@app.get("/api/export/status")
def export_status() -> dict[str, Any]:
    global _export_loop, _pusher, _log_export
    _gc_sessions()
    return {
        "loop": _export_loop.status if _export_loop else {"running": False},
        "log_forwarding": _log_export,
        "otlp_endpoint": getattr(_pusher, "_endpoint", None) if _pusher else None,
        "export_config": _export_config,
        "connected_devices": [
            _session_to_dict(sid, s) for sid, s in _sessions.items()
        ],
    }


def _resolve_export_sessions(
    session_ids: list[str],
) -> tuple[list[tuple[str, str, BigIPClient]], list[str]]:
    _gc_sessions()
    ids = session_ids or list(_sessions.keys())
    if not ids:
        raise HTTPException(
            status_code=400,
            detail="No BIG-IP devices connected. Add at least one device before starting export.",
        )
    metrics_clients: list[tuple[str, str, BigIPClient]] = []
    log_hosts: list[str] = []
    for sid in ids:
        sess = _get_session(sid)
        label = sess.label or _display_host(sess.host)
        if sess.export_metrics:
            endpoints = _resolve_metric_endpoints_for_session(sess)
            if not endpoints:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"No metric endpoints selected for {label}. "
                        "Configure API endpoints for this BIG-IP before starting export."
                    ),
                )
            metrics_clients.append(
                (_display_host(sess.host), sid, sess.client),
            )
        if sess.export_system_logs or (
            sess.export_logs
            and (sess.export_ltm_logs or sess.export_asm_logs or sess.export_afm_logs or sess.export_avr_logs)
        ):
            log_hosts.append(label)
    if not metrics_clients and not log_hosts:
        raise HTTPException(
            status_code=400,
            detail="No selected devices have metrics or log export enabled.",
        )
    return metrics_clients, log_hosts


def _start_export(body: ExportStartBody) -> dict[str, Any]:
    global _export_loop, _pusher, _log_export, _export_config
    if body.endpoints_by_session:
        _apply_metric_endpoints_by_session(body.endpoints_by_session)
    metrics_clients, log_hosts = _resolve_export_sessions(body.session_ids)
    resolved_session_ids = body.session_ids if body.session_ids else list(_sessions.keys())
    endpoints_by_session = {
        sid: _live_metric_endpoints_for_session(sid)
        for _, sid, _ in metrics_clients
    }
    endpoints_used = sorted({ep for eps in endpoints_by_session.values() for ep in eps})

    log_cfg = runtime_log_config()
    _log_export = {
        "active": bool(log_hosts),
        "hosts": log_hosts,
        "syslog_target": log_cfg["log_syslog_target"] if log_hosts else None,
        "hsl_target": log_cfg["log_hsl_target"] if log_hosts else None,
        "note": (
            "BIG-IP forwards ASM/AFM logs via syslog to "
            f"{log_cfg['log_syslog_target']} and LTM request/response logs via HSL to "
            f"{log_cfg['log_hsl_target']}. Attach logging profiles to virtual servers "
            "and ensure BIG-IP can reach both addresses."
            if log_hosts
            else None
        ),
    }

    response: dict[str, Any] = {
        "started": True,
        "log_forwarding": _log_export,
        "bigip_count": len({h for h, _, _ in metrics_clients} | set(log_hosts)),
        "metrics_hosts": [h for h, _, _ in metrics_clients],
        "log_hosts": log_hosts,
    }

    if metrics_clients:
        _stop_metrics_export()

        otlp = _resolve_otlp_endpoint(body.otlp_endpoint)
        _pusher = OTLPMetricsPusher(otlp)
        _export_loop = MetricsExportLoop(
            metrics_clients,
            _pusher,
            _live_metric_endpoints_for_session,
            poll_interval_sec=body.poll_interval_sec,
        )
        result = _export_loop.run_once()
        _export_loop.start_background()
        response.update(
            {
                "endpoints": sum(len(eps) for eps in endpoints_by_session.values()),
                "endpoints_by_session": endpoints_by_session,
                "first_run": result,
                "status": _export_loop.status,
            },
        )
    else:
        _stop_metrics_export()
        response["mode"] = "logs_only"

    _export_config = {
        "active": True,
        "session_ids": resolved_session_ids,
        "endpoints": endpoints_used,
        "endpoints_by_session": endpoints_by_session,
        "metrics_only": body.metrics_only,
        "modules": list(body.modules),
        "poll_interval_sec": body.poll_interval_sec,
        "otlp_endpoint": otlp if metrics_clients else _resolve_otlp_endpoint(body.otlp_endpoint),
    }
    _persist_runtime_state()
    return response


@app.post("/api/export/start")
def export_start(body: ExportStartBody) -> dict[str, Any]:
    return _start_export(body)


@app.post("/api/export/stop")
def export_stop() -> dict[str, Any]:
    global _log_export, _export_config
    _stop_metrics_export()
    _log_export = {
        "active": False,
        "hosts": [],
        "syslog_target": None,
        "hsl_target": None,
    }
    if _export_config:
        _export_config = {**_export_config, "active": False}
    else:
        _export_config = {"active": False}
    _persist_runtime_state()
    return {"stopped": True}


@app.post("/api/nuke")
def nuke_application(body: NukeBody) -> dict[str, Any]:
    """
    Full application reset: stop export, roll back BIG-IP log resources, clear
    sessions (memory + disk), and restore default collector exporter config.
    """
    global _sessions, _export_loop, _pusher, _log_export, _export_config
    global _collector_metric_exporters, _collector_log_exporters
    global _collector_export_metrics, _collector_export_logs

    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Nuke refused: set confirm=true in the JSON body (safety latch).",
        )

    _stop_metrics_export()
    _log_export = {
        "active": False,
        "hosts": [],
        "syslog_target": None,
        "hsl_target": None,
    }
    _export_config = None

    device_results: list[dict[str, Any]] = []
    for sid, sess in list(_sessions.items()):
        label = sess.label or _display_host(sess.host)
        entry: dict[str, Any] = {
            "session_id": sid,
            "host": sess.host,
            "label": label,
            "rollback": None,
            "logout": False,
            "errors": [],
        }
        if body.rollback_bigip:
            try:
                entry["rollback"] = rollback_log_resources(
                    sess.client,
                    delete_as3=True,
                    remove_system_syslog=True,
                    save_sys_config_after=True,
                )
                clear_session_log_resources(sess)
            except BigIPError as exc:
                entry["errors"].append(f"rollback: {exc}")
            except Exception as exc:  # noqa: BLE001
                entry["errors"].append(f"rollback: {exc}")
        try:
            sess.client.logout()
            entry["logout"] = True
        except Exception as exc:  # noqa: BLE001
            entry["errors"].append(f"logout: {exc}")
        _sessions.pop(sid, None)
        device_results.append(entry)

    store_clear = clear_store(remove_key=body.remove_encryption_key)
    # Ensure empty state is written if persistence stays enabled.
    _persist_runtime_state()

    collector_info: dict[str, Any] | None = None
    if body.reset_collector:
        _collector_metric_exporters = []
        _collector_log_exporters = []
        _collector_export_metrics = True
        _collector_export_logs = True
        try:
            path = write_collector_config(
                _collector_metric_exporters,
                _collector_log_exporters,
                export_metrics=_collector_export_metrics,
                export_logs=_collector_export_logs,
            )
            cfg = build_collector_config(
                _collector_metric_exporters,
                _collector_log_exporters,
                export_metrics=_collector_export_metrics,
                export_logs=_collector_export_logs,
            )
            restart_info: dict[str, Any] = {"attempted": False}
            if body.restart_collector and auto_restart_enabled():
                restart_info["attempted"] = True
                try:
                    restart_info.update(restart_collector(config_path=path))
                except BigIPError as exc:
                    restart_info.update(
                        {
                            "ok": False,
                            "error": str(exc),
                            "manual_hint": restart_hint(
                                collector_control_status()["restart_mode"]
                            ),
                        },
                    )
            collector_info = {
                "ok": True,
                "path": str(path),
                "metric_exporters": _collector_metric_exporters,
                "log_exporters": _collector_log_exporters,
                "collector_restart": restart_info,
            }
            collector_info["yaml_preview"] = _yaml_dump(cfg)[:2000]
        except ValueError as exc:
            collector_info = {"ok": False, "error": str(exc)}

    prometheus_info: dict[str, Any] | None = None
    if body.restart_prometheus and auto_restart_enabled():
        prometheus_info = {"attempted": True}
        try:
            prometheus_info.update(restart_prometheus())
        except BigIPError as exc:
            mode = collector_control_status()["restart_mode"]
            if mode == "docker":
                hint = (
                    f"docker compose -f {REPO_ROOT / 'docker-compose.yml'} up -d "
                    "--force-recreate prometheus"
                )
            elif mode == "kubernetes":
                hint = (
                    f"kubectl -n {os.environ.get('COLLECTOR_K8S_NAMESPACE', 'bigip-telemetry')} "
                    "delete pod -l app.kubernetes.io/name=prometheus --wait=true"
                )
            else:
                hint = "Recreate Prometheus manually for a fresh empty TSDB."
            prometheus_info.update({"ok": False, "error": str(exc), "manual_hint": hint})

    logger.warning(
        "Application nuke completed: %d device(s) processed, store_cleared=%s",
        len(device_results),
        store_clear,
    )
    return {
        "ok": True,
        "devices": device_results,
        "export_stopped": True,
        "sessions_remaining": len(_sessions),
        "store": store_clear,
        "collector": collector_info,
        "prometheus": prometheus_info,
    }


def _yaml_dump(cfg: dict[str, Any]) -> str:
    import yaml

    return yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False)


def _ui_setup_hint() -> str:
    return (
        "<!DOCTYPE html><html><head><title>BIG-IP Telemetry Exporter</title></head><body>"
        "<h1>BIG-IP Telemetry Exporter</h1>"
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

    def _favicon_candidates() -> list[Path]:
        return [
            FRONTEND_DIST / "favicon.svg",
            FRONTEND_DIST / "F5-logo-F5-rgb.svg",
            FAVICON_SVG,
        ]

    def _favicon_response() -> FileResponse:
        for candidate in _favicon_candidates():
            if candidate.is_file():
                return FileResponse(
                    candidate,
                    media_type="image/svg+xml",
                    headers={"Cache-Control": "no-cache"},
                )
        raise HTTPException(status_code=404, detail="Not Found")

    @app.get("/favicon.ico", include_in_schema=False, response_model=None)
    def ui_favicon_ico() -> FileResponse:
        return _favicon_response()

    @app.get("/favicon.svg", include_in_schema=False, response_model=None)
    def ui_favicon() -> FileResponse:
        return _favicon_response()

    @app.get("/F5-logo-F5-rgb.svg", include_in_schema=False, response_model=None)
    def ui_f5_logo() -> FileResponse:
        return _favicon_response()

    @app.get("/{ui_path:path}", include_in_schema=False, response_model=None)
    def ui_fallback(ui_path: str) -> FileResponse | HTMLResponse:
        if ui_path.startswith("api/") or ui_path == "api":
            raise HTTPException(status_code=404, detail="Not Found")
        if ui_path.startswith("assets/"):
            raise HTTPException(status_code=404, detail="Not Found")
        if ui_path in ("favicon.ico", "favicon.svg", "F5-logo-F5-rgb.svg"):
            return _favicon_response()
        if index_html.is_file():
            return FileResponse(index_html)
        return HTMLResponse(_ui_setup_hint(), status_code=200)


_register_ui_routes()
