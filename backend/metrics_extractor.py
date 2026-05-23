"""Convert BIG-IP iControl REST stats payloads into OTLP-friendly metric points."""

from __future__ import annotations

import os
import re
from typing import Any, Iterator

# BIG-IP nestedStats entry keys are often full selfLink URLs — avoid using them in metric names.
_URLISH = re.compile(r"^https?://|^https_", re.I)

# Skip rolling-average object paths (substring match on bigip.object, case-insensitive).
_DEFAULT_EXCLUDED_OBJECT_SUBSTRINGS: tuple[str, ...] = (
    "fiveminavg",
    "fivesecavg",
    "oneminavge",  # common BIG-IP / typo variant
    "oneminavg",
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


def _sanitize_name(part: str) -> str:
    part = part.replace("~", "").replace("/", "_").replace("-", "_").replace(".", "_")
    part = re.sub(r"[^a-zA-Z0-9_]", "_", part)
    part = re.sub(r"_+", "_", part).strip("_").lower()
    return part or "unknown"


def endpoint_metric_prefix(endpoint: str) -> str:
    """e.g. /mgmt/tm/sys/memory -> bigip_tm_sys_memory"""
    path = endpoint.strip("/").replace("mgmt/", "", 1)
    return "bigip_" + _sanitize_name(path)


def _entry_segment(key: str, index: int) -> str:
    """Short path segment for nestedStats entry keys (often URLs)."""
    raw = str(key).strip()
    if "://" in raw:
        tail = raw.rstrip("/").split("/")[-1] or f"entry_{index}"
        return _sanitize_name(tail)
    seg = _sanitize_name(raw)
    if _URLISH.match(seg) or seg.startswith("https_") or len(seg) > 40:
        return f"entry_{index}"
    return seg


def _compact_object_label(object_path: list[str]) -> str:
    """Short object label for Prometheus (avoids duplicating URL blobs)."""
    if not object_path:
        return "root"
    # Prefer non-entry_* segments when present; otherwise last entry slot.
    meaningful = [p for p in object_path if not p.startswith("entry_")]
    parts = meaningful[-2:] if meaningful else object_path[-1:]
    label = ".".join(parts)
    return label[:48] if len(label) > 48 else label


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
                for idx, (key, val) in enumerate(entries.items()):
                    yield from _walk_nested_stats(
                        val,
                        object_path=object_path + [_entry_segment(key, idx)],
                        endpoint=endpoint,
                        bigip_host=bigip_host,
                    )
            return
        if "entries" in obj and isinstance(obj["entries"], dict):
            for idx, (key, val) in enumerate(obj["entries"].items()):
                yield from _walk_nested_stats(
                    val,
                    object_path=object_path + [_entry_segment(key, idx)],
                    endpoint=endpoint,
                    bigip_host=bigip_host,
                )
            return
        for key, val in obj.items():
            if key in ("kind", "selfLink", "generation", "description", "isSubcollection"):
                continue
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                object_label = _compact_object_label(object_path)
                if is_excluded_bigip_object(object_label):
                    continue
                stat = _sanitize_name(str(key))
                metric_name = endpoint_metric_prefix(endpoint)
                attrs = {
                    "bigip.host": bigip_host,
                    "bigip.stat": stat,
                    "bigip.object": object_label,
                }
                yield metric_name, float(val), attrs
            elif isinstance(val, dict):
                yield from _walk_nested_stats(
                    val,
                    object_path=object_path + [_sanitize_name(str(key))],
                    endpoint=endpoint,
                    bigip_host=bigip_host,
                )


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
    return points
