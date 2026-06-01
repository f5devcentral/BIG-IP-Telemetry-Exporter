"""Remote syslog/HSL targets for BIG-IP log profile AS3 declarations."""

from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SYSLOG_PORT = 5140
DEFAULT_HSL_PORT = 5141

LOG_POOL_NAME = "bigip-telemetry-log-pool"
LOG_HSL_POOL_NAME = "bigip-telemetry-log-hsl-pool"
LOG_HSL_DEST_NAME = "bigip-telemetry-log-hsl-dest"
LOG_SYSLOG_DEST_NAME = "bigip-telemetry-log-syslog-dest"
LOG_PUBLISHER_NAME = "bigip-telemetry-log-publisher"

_LOG_HOST_ERROR = (
    "BIG-IP cannot use loopback for remote log forwarding. "
    "Set BIGIP_LOG_SYSLOG_HOST to an IP or hostname reachable from the BIG-IP "
    "(for example your Ubuntu LAN IP), or open the UI at http://<that-ip>:8001 "
    "instead of localhost."
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _normalize_host(value: str) -> str:
    return value.strip().split(":")[0].strip("[]")


def is_loopback_host(host: str) -> bool:
    """True when host is localhost/127.0.0.1 (invalid for BIG-IP remote log pools)."""
    normalized = _normalize_host(host).lower()
    if normalized in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return normalized == "localhost"


def _detect_outbound_ip() -> str | None:
    """Best-effort LAN IP via default-route UDP socket (Linux/macOS)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 1))
            ip = _normalize_host(sock.getsockname()[0])
            return None if is_loopback_host(ip) else ip
    except OSError:
        return None


def _host_ip_from_script() -> str | None:
    script = REPO_ROOT / "scripts" / "host-ip.sh"
    if not script.is_file():
        return None
    try:
        proc = subprocess.run(
            ["/bin/bash", str(script)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        ip = _normalize_host(proc.stdout.strip())
        if ip and not is_loopback_host(ip):
            return ip
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def resolve_syslog_host(*, browser_host: str | None = None) -> str:
    """
    IP/hostname the BIG-IP should use to reach the OTEL collector log receivers.

    Prefers explicit env vars, then a non-loopback browser Host, then auto-detect.
    Raises ValueError when only loopback would be used (AS3 rejects 127.0.0.1).
    """
    candidates: list[str] = []
    for key in ("BIGIP_LOG_SYSLOG_HOST", "HOST_IP", "ACCESS_HOST"):
        if value := os.environ.get(key, "").strip():
            candidates.append(_normalize_host(value))
    if browser_host:
        normalized = _normalize_host(browser_host)
        if not is_loopback_host(normalized):
            candidates.append(normalized)
    if detected := _detect_outbound_ip():
        candidates.append(detected)
    if script_ip := _host_ip_from_script():
        candidates.append(script_ip)

    for host in candidates:
        if host and not is_loopback_host(host):
            return host

    raise ValueError(_LOG_HOST_ERROR)


def syslog_host(*, browser_host: str | None = None) -> str:
    """Return resolved syslog/HSL target host (never loopback when resolvable)."""
    return resolve_syslog_host(browser_host=browser_host)


def syslog_port() -> int:
    return _env_int("BIGIP_LOG_SYSLOG_PORT", DEFAULT_SYSLOG_PORT)


def hsl_port() -> int:
    return _env_int("BIGIP_LOG_HSL_PORT", DEFAULT_HSL_PORT)


def syslog_target(*, browser_host: str | None = None) -> str:
    return f"{syslog_host(browser_host=browser_host)}:{syslog_port()}"


def hsl_target(*, browser_host: str | None = None) -> str:
    return f"{syslog_host(browser_host=browser_host)}:{hsl_port()}"


def runtime_log_config(*, browser_host: str | None = None) -> dict[str, str]:
    host = syslog_host(browser_host=browser_host)
    return {
        "log_syslog_host": host,
        "log_syslog_port": str(syslog_port()),
        "log_hsl_port": str(hsl_port()),
        "log_syslog_target": f"{host}:{syslog_port()}",
        "log_hsl_target": f"{host}:{hsl_port()}",
    }
