"""Persist BIG-IP sessions and export state across backend restarts."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

STORE_VERSION = 1
DEFAULT_STORE_DIR = Path.home() / ".config" / "bigip-telemetry-exporter"


def persist_enabled() -> bool:
    return os.environ.get("BIGIP_SESSION_PERSIST", "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def store_path() -> Path:
    raw = os.environ.get("BIGIP_SESSION_STORE_PATH", "").strip()
    if raw:
        return Path(raw)
    return DEFAULT_STORE_DIR / "sessions.json"


def _key_path() -> Path:
    env_key_file = os.environ.get("BIGIP_SESSION_KEY_FILE", "").strip()
    if env_key_file:
        return Path(env_key_file)
    return store_path().with_suffix(".key")


def _fernet() -> Fernet:
    explicit = os.environ.get("BIGIP_SESSION_ENCRYPTION_KEY", "").strip()
    if explicit:
        key = explicit.encode("utf-8")
    else:
        key_file = _key_path()
        if key_file.is_file():
            key = key_file.read_text(encoding="utf-8").strip().encode("utf-8")
        else:
            key = Fernet.generate_key()
            key_file.parent.mkdir(parents=True, exist_ok=True)
            key_file.write_text(key.decode("utf-8"), encoding="utf-8")
            try:
                key_file.chmod(0o600)
            except OSError:
                pass
            logger.info("Created session encryption key at %s", key_file)
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Could not decrypt stored session credentials") from exc


def session_record_from_dict(session_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Build a stored session record from live session metadata."""
    return {
        "session_id": session_id,
        "host": data["host"],
        "username": data["username"],
        "password": encrypt_secret(data["password"]),
        "verify_tls": bool(data.get("verify_tls", False)),
        "label": data.get("label") or "",
        "created": float(data.get("created", 0)),
        "token_timeout_sec": int(data.get("token_timeout_sec", 1200)),
        "warning": data.get("warning"),
        "request_log_profile": data.get("request_log_profile"),
        "request_log_profile_created": data.get("request_log_profile_created"),
        "asm_log_profile": data.get("asm_log_profile"),
        "asm_log_profile_created": data.get("asm_log_profile_created"),
        "afm_log_profile": data.get("afm_log_profile"),
        "afm_log_profile_created": data.get("afm_log_profile_created"),
        "http_analytics_profile": data.get("http_analytics_profile"),
        "http_analytics_profile_created": data.get("http_analytics_profile_created"),
        "tcp_analytics_profile": data.get("tcp_analytics_profile"),
        "tcp_analytics_profile_created": data.get("tcp_analytics_profile_created"),
        "log_syslog_target": data.get("log_syslog_target"),
        "log_hsl_target": data.get("log_hsl_target"),
        "system_syslog_target": data.get("system_syslog_target"),
        "export_metrics": bool(data.get("export_metrics", True)),
        "export_logs": bool(data.get("export_logs", True)),
        "export_system_logs": bool(data.get("export_system_logs", False)),
        "export_ltm_logs": bool(data.get("export_ltm_logs", True)),
        "export_asm_logs": bool(data.get("export_asm_logs", True)),
        "export_afm_logs": bool(data.get("export_afm_logs", True)),
        "export_avr_logs": bool(data.get("export_avr_logs", True)),
        "prov_ltm": bool(data.get("prov_ltm", True)),
        "prov_asm": bool(data.get("prov_asm", False)),
        "prov_afm": bool(data.get("prov_afm", False)),
        "prov_avr": bool(data.get("prov_avr", False)),
        "metric_endpoints": [
            str(ep) for ep in (data.get("metric_endpoints") or []) if str(ep).strip()
        ],
        "tmctl_tables": [
            str(t) for t in (data.get("tmctl_tables") or []) if str(t).strip()
        ],
    }


def save_store(
    sessions: list[dict[str, Any]],
    export_state: dict[str, Any] | None,
) -> None:
    if not persist_enabled():
        return
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STORE_VERSION,
        "sessions": sessions,
        "export": export_state,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_store() -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if not persist_enabled():
        return [], None
    path = store_path()
    if not path.is_file():
        return [], None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read session store %s: %s", path, exc)
        return [], None
    if not isinstance(payload, dict):
        return [], None
    sessions = payload.get("sessions")
    export_state = payload.get("export")
    if not isinstance(sessions, list):
        sessions = []
    if export_state is not None and not isinstance(export_state, dict):
        export_state = None
    return sessions, export_state


def clear_store(*, remove_key: bool = False) -> dict[str, bool]:
    """Delete persisted session + export state (and optionally the encryption key)."""
    removed_store = False
    removed_key = False
    path = store_path()
    if path.is_file():
        try:
            path.unlink()
            removed_store = True
        except OSError as exc:
            logger.warning("Could not delete session store %s: %s", path, exc)
    if remove_key:
        key_file = _key_path()
        if key_file.is_file():
            try:
                key_file.unlink()
                removed_key = True
            except OSError as exc:
                logger.warning("Could not delete session key %s: %s", key_file, exc)
    return {"removed_store": removed_store, "removed_key": removed_key}


def stored_credentials(record: dict[str, Any]) -> tuple[str, str, str, bool]:
    password = decrypt_secret(str(record["password"]))
    return (
        str(record["host"]),
        str(record["username"]),
        password,
        bool(record.get("verify_tls", False)),
    )
