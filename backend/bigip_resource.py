"""Shared helpers for idempotent BIG-IP configuration objects."""

from __future__ import annotations

from typing import Any

from backend.bigip_client import BigIPClient, BigIPError


def is_not_found(exc: BigIPError) -> bool:
    return "404" in str(exc)


def is_invalid_path(exc: BigIPError) -> bool:
    text = str(exc)
    return "501" in text or "Invalid Path" in text


def path_from_self_link(self_link: str) -> str | None:
    """Extract /mgmt/... path from a BIG-IP selfLink URL."""
    if not self_link:
        return None
    idx = self_link.find("/mgmt/")
    if idx < 0:
        return None
    return self_link[idx:].split("?", 1)[0]


def find_collection_item(
    client: BigIPClient,
    collection_path: str,
    *,
    name: str,
    partition: str = "Common",
) -> dict[str, Any] | None:
    data = client.get(collection_path)
    if not isinstance(data, dict):
        return None
    items = data.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        item_name = str(item.get("name", ""))
        item_part = str(item.get("partition") or "Common")
        if item_name == name and item_part == partition:
            return item
    return None


def ensure_config_object(
    client: BigIPClient,
    *,
    collection_path: str,
    instance_path: str,
    create_body: dict[str, Any],
    patch_body: dict[str, Any],
) -> bool:
    """Create the object if missing, otherwise patch. Returns True when newly created."""
    try:
        client.get(instance_path)
    except BigIPError as exc:
        if not (is_not_found(exc) or is_invalid_path(exc)):
            raise
        client.post(collection_path, json_body=create_body)
        return True

    client.patch(instance_path, json_body=patch_body)
    return False
