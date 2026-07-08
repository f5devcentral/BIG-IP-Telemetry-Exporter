"""Convert BIG-IP iControl REST stats payloads into OTLP-friendly metric points."""

from __future__ import annotations

import os
import re
from typing import Any, Iterator

# BIG-IP nestedStats entry keys are often full selfLink URLs — avoid using them in metric names.
_URLISH = re.compile(r"^https?://|^https_", re.I)

# Skip rolling-average stats/objects (substring match, case-insensitive).
_DEFAULT_EXCLUDED_OBJECT_SUBSTRINGS: tuple[str, ...] = (
    "fiveminavg",
    "fivesecavg",
    "oneminavge",  # common BIG-IP / typo variant
    "oneminavg",
)

_SKIP_DICT_KEYS = frozenset(
    {"kind", "selfLink", "generation", "description", "isSubcollection"},
)


def _excluded_object_substrings() -> tuple[str, ...]:
    raw = os.environ.get("BIGIP_EXCLUDE_OBJECT_PATTERNS", "").strip()
    if raw:
        return tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    return _DEFAULT_EXCLUDED_OBJECT_SUBSTRINGS


def is_excluded_bigip_object(object_label: str) -> bool:
    """True if this bigip.object value should not be exported."""
    label = object_label.lower()
    return any(sub in label for sub in _excluded_object_substrings())


def is_excluded_bigip_stat(stat: str) -> bool:
    """True if this stat field name should not be exported."""
    label = stat.lower()
    return any(sub in label for sub in _excluded_object_substrings())


def _sanitize_name(part: str) -> str:
    part = part.replace("~", "").replace("/", "_").replace("-", "_").replace(".", "_")
    part = re.sub(r"[^a-zA-Z0-9_]", "_", part)
    part = re.sub(r"_+", "_", part).strip("_").lower()
    return part or "unknown"


def endpoint_metric_prefix(endpoint: str) -> str:
    """e.g. /mgmt/tm/sys/memory -> bigip_tm_sys_memory"""
    path = endpoint.strip("/").replace("mgmt/", "", 1)
    return "bigip_" + _sanitize_name(path)


def metric_name_for_stat(endpoint: str, stat: str) -> str:
    """One OTLP metric name per stat field, scoped to the REST endpoint path."""
    return f"{endpoint_metric_prefix(endpoint)}_{_sanitize_name(stat)}"


def _entry_segment(key: str, index: int) -> str:
    """Short path segment for nestedStats entry keys (often URLs)."""
    raw = str(key).strip()
    if "://" in raw:
        parts = [p for p in raw.rstrip("/").split("/") if p]
        if len(parts) >= 2 and parts[-1].lower() == "stats":
            name_part = parts[-2]
            if "~" in name_part:
                return _sanitize_name(name_part.split("~")[-1])
            return _sanitize_name(name_part)
        tail = parts[-1] if parts else f"entry_{index}"
        return _sanitize_name(tail)
    seg = _sanitize_name(raw)
    if _URLISH.match(seg) or seg.startswith("https_") or len(seg) > 40:
        return f"entry_{index}"
    return seg


def _walk_collection_entries(
    entries: dict[str, Any],
    *,
    object_path: list[str],
    endpoint: str,
    bigip_host: str,
) -> Iterator[tuple[str, float, dict[str, str]]]:
    """Walk nestedStats/entries maps; stat fields stay off the object path."""
    for idx, (key, val) in enumerate(entries.items()):
        if isinstance(val, dict):
            leaf = _bigip_value_leaf(val)
            if leaf is not None:
                yield from _emit_numeric(
                    endpoint=endpoint,
                    bigip_host=bigip_host,
                    object_path=object_path,
                    stat=str(key),
                    value=leaf,
                )
                continue
        yield from _walk_nested_stats(
            val,
            object_path=object_path + [_entry_segment(key, idx)],
            endpoint=endpoint,
            bigip_host=bigip_host,
        )


def _compact_object_label(object_path: list[str]) -> str:
    """Short object label (virtual server, pool member slot, etc.)."""
    if not object_path:
        return "root"
    meaningful = [p for p in object_path if not p.startswith("entry_")]
    parts = meaningful[-2:] if meaningful else object_path[-1:]
    label = ".".join(parts)
    return label[:48] if len(label) > 48 else label


def _bigip_value_leaf(obj: dict[str, Any]) -> float | None:
    """BIG-IP stats fields are usually ``{"value": <number>}`` (sometimes with description)."""
    value = obj.get("value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _emit_numeric(
    *,
    endpoint: str,
    bigip_host: str,
    object_path: list[str],
    stat: str,
    value: float,
) -> Iterator[tuple[str, float, dict[str, str]]]:
    stat_key = _sanitize_name(stat)
    if is_excluded_bigip_stat(stat_key):
        return
    object_label = _compact_object_label(object_path)
    if is_excluded_bigip_object(object_label):
        return
    attrs = {
        "bigip.host": bigip_host,
        "bigip.object": object_label,
    }
    yield metric_name_for_stat(endpoint, stat_key), value, attrs


def _walk_nested_stats(
    obj: Any,
    *,
    object_path: list[str],
    endpoint: str,
    bigip_host: str,
) -> Iterator[tuple[str, float, dict[str, str]]]:
    if isinstance(obj, dict):
        if "nestedStats" in obj:
            entries = obj.get("nestedStats", {}).get("entries", {})
            if isinstance(entries, dict):
                yield from _walk_collection_entries(
                    entries,
                    object_path=object_path,
                    endpoint=endpoint,
                    bigip_host=bigip_host,
                )
            return
        if "entries" in obj and isinstance(obj["entries"], dict):
            yield from _walk_collection_entries(
                obj["entries"],
                object_path=object_path,
                endpoint=endpoint,
                bigip_host=bigip_host,
            )
            return
        for key, val in obj.items():
            if key in _SKIP_DICT_KEYS:
                continue
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                yield from _emit_numeric(
                    endpoint=endpoint,
                    bigip_host=bigip_host,
                    object_path=object_path,
                    stat=str(key),
                    value=float(val),
                )
            elif isinstance(val, dict):
                leaf = _bigip_value_leaf(val)
                if leaf is not None:
                    yield from _emit_numeric(
                        endpoint=endpoint,
                        bigip_host=bigip_host,
                        object_path=object_path,
                        stat=str(key),
                        value=leaf,
                    )
                else:
                    yield from _walk_nested_stats(
                        val,
                        object_path=object_path + [_sanitize_name(str(key))],
                        endpoint=endpoint,
                        bigip_host=bigip_host,
                    )


_ROLLUP_SUFFIXES = (
    "_device_avg",
    "_device_max",
    "_host_avg",
    "_host_max",
    "_tmm_avg",
    "_tmm_max",
)


def _is_rollup_metric(name: str) -> bool:
    return any(name.endswith(suffix) for suffix in _ROLLUP_SUFFIXES)


def _rollup_object_group(object_label: str) -> str | None:
    """Map a bigip.object label to a rollup bucket (cpu, host memory, TMM memory)."""
    label = object_label.lower()
    if "cpuinfo" in label:
        return "cpu"
    if label.startswith("memory_host") or ".memory_host" in label:
        return "memory_host"
    if label.startswith("memory_tmm") or ".memory_tmm" in label or "memory_tmm" in label:
        return "memory_tmm"
    return None


def _rollup_metric_names(base_name: str, group: str) -> tuple[str, str]:
    if group == "cpu":
        return f"{base_name}_device_avg", f"{base_name}_device_max"
    if group == "memory_host":
        return f"{base_name}_host_avg", f"{base_name}_host_max"
    if group == "memory_tmm":
        return f"{base_name}_tmm_avg", f"{base_name}_tmm_max"
    raise ValueError(f"unknown rollup group: {group}")


def _append_device_rollups(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Emit device-level avg/max rollups for per-core CPU and host/TMM memory objects."""
    buckets: dict[tuple[str, str, str], list[float]] = {}
    for pt in points:
        name = str(pt["name"])
        if _is_rollup_metric(name):
            continue
        attrs = pt.get("attributes") or {}
        obj = attrs.get("bigip.object")
        host = str(attrs.get("bigip.host", "unknown"))
        if not obj:
            continue
        group = _rollup_object_group(str(obj))
        if group is None:
            continue
        key = (name, group, host)
        buckets.setdefault(key, []).append(float(pt["value"]))

    rollups: list[dict[str, Any]] = []
    for (name, group, host), values in buckets.items():
        if not values:
            continue
        avg_name, max_name = _rollup_metric_names(name, group)
        host_attrs = {"bigip.host": host}
        rollups.append(
            {"name": avg_name, "value": sum(values) / len(values), "attributes": host_attrs},
        )
        rollups.append({"name": max_name, "value": max(values), "attributes": host_attrs})
    return rollups


def extract_metrics(
    endpoint: str,
    payload: Any,
    *,
    bigip_host: str = "unknown",
) -> list[dict[str, Any]]:
    """Return list of {name, value, attributes} dicts."""
    points: list[dict[str, Any]] = []
    for name, value, attrs in _walk_nested_stats(
        payload,
        object_path=[],
        endpoint=endpoint,
        bigip_host=bigip_host,
    ):
        points.append({"name": name, "value": value, "attributes": attrs})
    points.extend(_append_device_rollups(points))
    return points
