"""Parse BIG-IP tmctl table listings and CSV metric rows for OTLP export."""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from backend.bigip_client import BigIPError

# Strict allowlist for table names passed into bash utilCmdArgs.
_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")

# Curated tmctl stats tables exposed in the UI (TMCTL TABLES module).
TMCTL_STATS_TABLES: tuple[str, ...] = (
    "virtual_server_cpu_stat",
    "tmm_stat",
    "plane_proc_stat",
    "plane_cpu_stat",
    "mcp_worker_stats",
    "proc_stat",
    "asm_memory_util_stats",
    "asm_cloud_services_stats",
    "fw_sendtovirtual_stats",
    "fw_current_state_stat",
    "disk_latency_stat",
)
_TMCTL_STATS_TABLE_SET = frozenset(TMCTL_STATS_TABLES)


def list_tmctl_stats_tables() -> list[str]:
    """Return the fixed catalog of tmctl stats tables exposed in the UI."""
    return list(TMCTL_STATS_TABLES)


def validate_tmctl_table_name(name: str) -> str:
    """Return a sanitized table name or raise BigIPError."""
    cleaned = (name or "").strip()
    if not cleaned or not _TABLE_NAME_RE.match(cleaned):
        raise BigIPError(
            f"Invalid tmctl table name {name!r}; "
            "only letters, digits, and underscore are allowed.",
        )
    return cleaned


def validate_tmctl_stats_table(name: str) -> str:
    """Ensure the table is in the curated stats catalog."""
    cleaned = validate_tmctl_table_name(name)
    if cleaned not in _TMCTL_STATS_TABLE_SET:
        raise BigIPError(
            f"tmctl table {name!r} is not in the supported stats catalog.",
        )
    return cleaned


def parse_tmctl_table_list(text: str) -> list[str]:
    """
    Parse ``tmctl -a`` output into table names.

    Output is typically one table name per line; ignore blanks, comments,
    and decorative separators.
    """
    tables: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Some builds print "table_name ..." with trailing description.
        token = line.split()[0].strip()
        if not _TABLE_NAME_RE.match(token):
            continue
        if token in seen:
            continue
        seen.add(token)
        tables.append(token)
    return tables


def _parse_number(raw: str) -> float | None:
    value = (raw or "").strip()
    if not value or value in {".", "-", "N/A", "n/a", "null"}:
        return None
    # Strip common unit suffixes tmctl occasionally prints (e.g. 1.5G).
    if value[-1:] in "KkMmGgTtBb" and _NUMERIC_RE.match(value[:-1]):
        mult = {
            "K": 1024.0,
            "k": 1024.0,
            "M": 1024.0**2,
            "m": 1024.0**2,
            "G": 1024.0**3,
            "g": 1024.0**3,
            "T": 1024.0**4,
            "t": 1024.0**4,
            "B": 1.0,
            "b": 1.0,
        }[value[-1]]
        return float(value[:-1]) * mult
    if not _NUMERIC_RE.match(value):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _sanitize_metric_part(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", value.strip())[:120] or "x"


def _row_identity_attrs(headers: list[str], row: dict[str, str]) -> dict[str, str]:
    """Pick columns that identify a row (name/slot/tmid/etc.)."""
    attrs: dict[str, str] = {}
    preferred = (
        "name",
        "slot",
        "tmid",
        "tm_name",
        "proc",
        "pid",
        "type",
        "key",
        "id",
    )
    for key in preferred:
        if key in row and row[key].strip():
            attrs[f"tmctl.{key}"] = row[key].strip()[:200]
    # If nothing preferred, use first non-numeric column as object id.
    if not attrs:
        for header in headers:
            cell = row.get(header, "").strip()
            if cell and _parse_number(cell) is None:
                attrs["tmctl.object"] = cell[:200]
                break
    return attrs


def extract_tmctl_metrics(
    table: str,
    csv_text: str,
    *,
    bigip_host: str,
) -> list[dict[str, Any]]:
    """
    Convert ``tmctl -c <table>`` CSV text into OTLP-friendly metric points.

    Metric names: ``bigip_tmctl_<table>_<column>``.
    Row identity columns become attributes (``tmctl.name``, ``tmctl.slot``, …).
    """
    table_name = validate_tmctl_stats_table(table)
    text = (csv_text or "").strip()
    if not text:
        return []

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []
    headers = [h.strip() for h in reader.fieldnames if h and h.strip()]
    if not headers:
        return []

    points: list[dict[str, Any]] = []
    table_safe = _sanitize_metric_part(table_name)
    # Prefer identity columns as labels only (not separate gauges).
    identity_headers = {
        "name",
        "slot",
        "tmid",
        "tm_name",
        "proc",
        "pid",
        "type",
        "key",
        "id",
    }
    for row_raw in reader:
        row = {
            (k or "").strip(): (v if v is not None else "").strip()
            for k, v in row_raw.items()
            if k is not None
        }
        identity = _row_identity_attrs(headers, row)
        base_attrs = {"bigip.host": bigip_host, "tmctl.table": table_name, **identity}
        for header in headers:
            if header in identity_headers:
                continue
            numeric = _parse_number(row.get(header, ""))
            if numeric is None:
                continue
            col_safe = _sanitize_metric_part(header)
            points.append(
                {
                    "name": f"bigip_tmctl_{table_safe}_{col_safe}",
                    "value": numeric,
                    "attributes": dict(base_attrs),
                }
            )
    return points


def build_tmctl_query_command(table: str) -> str:
    """Bash -c payload that dumps a single table as CSV."""
    name = validate_tmctl_stats_table(table)
    # -c = CSV; safer for parsing across tmctl versions.
    return f"-c 'tmctl -c {name}'"
