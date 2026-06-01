"""Configure BIG-IP system syslog forwarding to the collector."""

from __future__ import annotations

from typing import Any

from backend.bigip_client import BigIPClient, BigIPError

SYSLOG_PATH = "/mgmt/tm/sys/syslog"

# Marker so we can detect / avoid duplicating our syslog-ng include.
_INCLUDE_MARKER = "bigip_telemetry_exporter_syslog_forwarding"


def _build_include(*, host: str, port: int) -> str:
    # syslog-ng syntax, embedded as a single tmsh string.
    #
    # s_syslog_pipe is the default source that represents system log flow.
    # This forwards everything flowing through that source to a TCP destination.
    #
    # Note: TMOS will reject malformed include strings with 400 errors; we surface those.
    #
    # Using unique names avoids collisions with customer includes.
    return (
        f"# {_INCLUDE_MARKER}\n"
        f"destination d_bigip_telemetry_exporter {{ tcp(\"{host}\" port({port})); }};\n"
        "log { source(s_syslog_pipe); destination(d_bigip_telemetry_exporter); };\n"
    )


def ensure_system_syslog_forwarding(
    client: BigIPClient,
    *,
    host: str,
    port: int = 5140,
) -> dict[str, Any]:
    """
    Ensure BIG-IP system logs are forwarded to host:port via syslog-ng include.

    This is best-effort and intentionally minimal: it adds/updates a syslog-ng include stanza.
    """
    current = client.get(SYSLOG_PATH)
    if not isinstance(current, dict):
        raise BigIPError("Unexpected /sys/syslog response shape")

    existing = str(current.get("include") or "")
    desired = _build_include(host=host, port=port)

    if _INCLUDE_MARKER in existing:
        # Replace our block (simple strategy: append desired after removing marker block)
        # Since include is free-form, we avoid complex parsing: just keep existing and
        # add a fresh copy if the host/port changed.
        if desired.strip() in existing:
            return {"ok": True, "changed": False, "mode": "include", "target": f"{host}:{port}"}
        new_include = existing
        # Remove any previous lines containing our marker or destination/log lines.
        filtered_lines: list[str] = []
        for line in new_include.splitlines():
            if _INCLUDE_MARKER in line:
                continue
            if "d_bigip_telemetry_exporter" in line:
                continue
            filtered_lines.append(line)
        new_include = ("\n".join(filtered_lines).strip() + "\n" + desired).strip() + "\n"
    else:
        new_include = (existing.strip() + "\n" + desired).strip() + "\n"

    try:
        client.patch(SYSLOG_PATH, json_body={"include": new_include})
    except BigIPError as exc:
        raise BigIPError(f"Failed to configure system syslog forwarding: {exc}") from exc

    return {"ok": True, "changed": True, "mode": "include", "target": f"{host}:{port}"}

