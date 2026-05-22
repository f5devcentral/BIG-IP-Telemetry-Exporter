"""Convert BIG-IP iControl REST stats payloads into OTLP-friendly metric points."""

from __future__ import annotations

import re
from typing import Any, Iterator


def _sanitize_name(part: str) -> str:
    part = part.replace("~", "").replace("/", "_").replace("-", "_").replace(".", "_")
    part = re.sub(r"[^a-zA-Z0-9_]", "_", part)
    part = re.sub(r"_+", "_", part).strip("_").lower()
    return part or "unknown"


def endpoint_metric_prefix(endpoint: str) -> str:
    """e.g. /mgmt/tm/ltm/virtual/stats -> bigip_tm_ltm_virtual_stats"""
    path = endpoint.strip("/").replace("mgmt/", "", 1)
    return "bigip_" + _sanitize_name(path)


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
                for key, val in entries.items():
                    yield from _walk_nested_stats(
                        val,
                        object_path=object_path + [_sanitize_name(str(key))],
                        endpoint=endpoint,
                        bigip_host=bigip_host,
                    )
            return
        if "entries" in obj and isinstance(obj["entries"], dict):
            for key, val in obj["entries"].items():
                yield from _walk_nested_stats(
                    val,
                    object_path=object_path + [_sanitize_name(str(key))],
                    endpoint=endpoint,
                    bigip_host=bigip_host,
                )
            return
        for key, val in obj.items():
            if key in ("kind", "selfLink", "generation", "description", "isSubcollection"):
                continue
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                name_parts = [endpoint_metric_prefix(endpoint), *object_path, _sanitize_name(str(key))]
                metric_name = ".".join(p for p in name_parts if p)
                attrs = {
                    "bigip.endpoint": endpoint,
                    "bigip.object": ".".join(object_path) if object_path else "root",
                    "bigip.host": bigip_host,
                    "bigip.management_ip": bigip_host,
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
