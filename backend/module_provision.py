"""BIG-IP licensed module provisioning checks via iControl REST."""

from __future__ import annotations

from typing import Any

from backend.bigip_client import BigIPClient, BigIPError

PROVISION_COLLECTION = "/mgmt/tm/sys/provision"


def level_is_provisioned(level: str | None) -> bool:
    """Return True when module level is anything other than none."""
    return (level or "none").strip().lower() != "none"


def get_module_provision_level(client: BigIPClient, module: str) -> str | None:
    """Return provision level for a module name, or None if unknown/unlicensed."""
    name = module.strip().lower()
    if not name:
        return None
    try:
        data = client.get(f"{PROVISION_COLLECTION}/{name}")
    except BigIPError:
        return None
    if isinstance(data, dict) and "level" in data:
        value = data.get("level")
        return str(value) if value is not None else "none"
    return None


def is_module_provisioned(client: BigIPClient, module: str) -> bool:
    """True when the module exists and provisioning level is not none."""
    level = get_module_provision_level(client, module)
    if level is None:
        return _provisioned_from_collection(client, module)
    return level_is_provisioned(level)


def _provisioned_from_collection(client: BigIPClient, module: str) -> bool:
    """Fallback: scan provision collection items for module name."""
    name = module.strip().lower()
    try:
        data = client.get(PROVISION_COLLECTION)
    except BigIPError:
        return False
    items: list[Any] = []
    if isinstance(data, dict):
        raw = data.get("items")
        if isinstance(raw, list):
            items = raw
    for item in items:
        if not isinstance(item, dict):
            continue
        item_name = str(item.get("name", "")).strip().lower()
        if item_name == name:
            return level_is_provisioned(str(item.get("level")))
    return False
