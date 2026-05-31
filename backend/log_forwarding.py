"""Remote syslog/HSL targets for BIG-IP log profile AS3 declarations."""

from __future__ import annotations

import os

DEFAULT_SYSLOG_PORT = 5140
DEFAULT_HSL_PORT = 5141

LOG_POOL_NAME = "bigip-telemetry-log-pool"
LOG_HSL_POOL_NAME = "bigip-telemetry-log-hsl-pool"
LOG_HSL_DEST_NAME = "bigip-telemetry-log-hsl-dest"
LOG_SYSLOG_DEST_NAME = "bigip-telemetry-log-syslog-dest"
LOG_PUBLISHER_NAME = "bigip-telemetry-log-publisher"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def syslog_host() -> str:
    """IP/hostname BIG-IP uses to reach the OTEL collector syslog receiver."""
    for key in ("BIGIP_LOG_SYSLOG_HOST", "HOST_IP", "ACCESS_HOST"):
        if value := os.environ.get(key, "").strip():
            return value.split(":")[0]
    return "127.0.0.1"


def syslog_port() -> int:
    return _env_int("BIGIP_LOG_SYSLOG_PORT", DEFAULT_SYSLOG_PORT)


def hsl_port() -> int:
    return _env_int("BIGIP_LOG_HSL_PORT", DEFAULT_HSL_PORT)


def syslog_target() -> str:
    return f"{syslog_host()}:{syslog_port()}"


def hsl_target() -> str:
    return f"{syslog_host()}:{hsl_port()}"


def runtime_log_config() -> dict[str, str]:
    return {
        "log_syslog_host": syslog_host(),
        "log_syslog_port": str(syslog_port()),
        "log_hsl_port": str(hsl_port()),
        "log_syslog_target": syslog_target(),
        "log_hsl_target": hsl_target(),
    }
