"""ASM and AFM security log profiles (request-type / network event capture)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from backend.bigip_client import BigIPClient
from backend.bigip_resource import ensure_config_object, is_not_found
from backend.module_provision import is_module_provisioned
from backend.log_templates import AFM_NETWORK_EVENT_TEMPLATE, REQUEST_EVENT_TEMPLATE

# Re-export for tests and documentation.
__all__ = [
    "SecurityLogProfileResult",
    "ensure_afm_log_profile",
    "ensure_asm_log_profile",
]

PROFILE_COLLECTION = "/mgmt/tm/security/log/profile"
DEFAULT_PARTITION = "Common"
DEFAULT_ASM_NAME = "bigip-metrics-asm-log"
DEFAULT_AFM_NAME = "bigip-metrics-afm-log"
ASM_DESCRIPTION = (
    "Created by BIG-IP Metrics Exporter. Attach as a Security Log Profile on virtual servers "
    "for ASM (Application Security); logs all requests and responses locally for future OTLP export."
)
AFM_DESCRIPTION = (
    "Created by BIG-IP Metrics Exporter. Attach as a Security Log Profile on virtual servers "
    "for AFM (Network Firewall); logs all network firewall event categories for future OTLP export."
)


@dataclass(frozen=True)
class SecurityLogProfileResult:
    full_name: str
    instance_path: str
    created: bool
    module: str


def _partition() -> str:
    return os.environ.get("BIGIP_LOG_PROFILE_PARTITION", DEFAULT_PARTITION).strip() or "Common"


def _asm_name() -> str:
    return os.environ.get("BIGIP_ASM_LOG_PROFILE_NAME", DEFAULT_ASM_NAME).strip()


def _afm_name() -> str:
    return os.environ.get("BIGIP_AFM_LOG_PROFILE_NAME", DEFAULT_AFM_NAME).strip()


def _full_name(partition: str, name: str) -> str:
    return f"/{partition}/{name}"


def _instance_path(partition: str, name: str) -> str:
    return f"{PROFILE_COLLECTION}/~{partition}~{name}"


def _asm_auto_create() -> bool:
    return os.environ.get("BIGIP_ASM_LOG_AUTO_CREATE", "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _afm_auto_create() -> bool:
    return os.environ.get("BIGIP_AFM_LOG_AUTO_CREATE", "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _asm_application_body() -> dict:
    """Application sub-profile body (format/filter are structures, not JSON arrays)."""
    return {
        "name": "application",
        "localStorage": "enabled",
        "guaranteeLogging": "enabled",
        "guaranteeResponseLogging": "enabled",
        "responseLogging": "all",
        "logicOperation": "or",
        "filter": {
            "name": "request-type",
            "values": ["all"],
        },
        "format": {
            "type": "user-defined",
            "userString": REQUEST_EVENT_TEMPLATE,
        },
    }


def _afm_network_body() -> dict:
    """Network sub-profile body (filter/format are structures on BIG-IP)."""
    return {
        "name": "network",
        "filter": {
            "logAclMatchAccept": "enabled",
            "logAclMatchDrop": "enabled",
            "logAclMatchReject": "enabled",
            "logAclToBoxDeny": "enabled",
            "logGeoAlways": "enabled",
            "logIpErrors": "enabled",
            "logTcpErrors": "enabled",
            "logTcpEvents": "enabled",
            "logTranslationFields": "enabled",
            "logUserAlways": "enabled",
        },
        "format": {
            "type": "user-defined",
            "userDefined": AFM_NETWORK_EVENT_TEMPLATE,
        },
    }


def _profile_shell(*, partition: str, name: str, description: str) -> dict:
    return {"name": name, "partition": partition, "description": description}


def _ensure_log_profile_subcollection(
    client: BigIPClient,
    *,
    partition: str,
    name: str,
    description: str,
    subcollection: str,
    sub_body: dict[str, object],
) -> bool:
    """Create log profile shell, then configure application or network sub-profile."""
    path = _instance_path(partition, name)
    shell = _profile_shell(partition=partition, name=name, description=description)
    created = ensure_config_object(
        client,
        collection_path=PROFILE_COLLECTION,
        instance_path=path,
        create_body=shell,
        patch_body={"description": description},
    )

    sub_instance = f"{path}/{subcollection}/{subcollection}"
    sub_collection = f"{path}/{subcollection}"
    try:
        client.get(sub_instance)
    except BigIPError as exc:
        if not is_not_found(exc):
            raise
        client.post(sub_collection, json_body=sub_body)
        return created

    client.patch(sub_instance, json_body=sub_body)
    return created


def ensure_asm_log_profile(
    client: BigIPClient,
    *,
    partition: str | None = None,
    name: str | None = None,
) -> SecurityLogProfileResult | None:
    """ASM security log profile with storage filter request-type all."""
    if not is_module_provisioned(client, "asm"):
        return None
    part = partition or _partition()
    prof = name or _asm_name()
    path = _instance_path(part, prof)
    full = _full_name(part, prof)
    if not _asm_auto_create():
        return SecurityLogProfileResult(
            full_name=full, instance_path=path, created=False, module="ASM"
        )

    created = _ensure_log_profile_subcollection(
        client,
        partition=part,
        name=prof,
        description=ASM_DESCRIPTION,
        subcollection="application",
        sub_body=_asm_application_body(),
    )
    return SecurityLogProfileResult(
        full_name=full, instance_path=path, created=created, module="ASM"
    )


def ensure_afm_log_profile(
    client: BigIPClient,
    *,
    partition: str | None = None,
    name: str | None = None,
) -> SecurityLogProfileResult | None:
    """AFM network security log profile with all network firewall log categories enabled."""
    if not is_module_provisioned(client, "afm"):
        return None
    part = partition or _partition()
    prof = name or _afm_name()
    path = _instance_path(part, prof)
    full = _full_name(part, prof)
    if not _afm_auto_create():
        return SecurityLogProfileResult(
            full_name=full, instance_path=path, created=False, module="AFM"
        )

    created = _ensure_log_profile_subcollection(
        client,
        partition=part,
        name=prof,
        description=AFM_DESCRIPTION,
        subcollection="network",
        sub_body=_afm_network_body(),
    )
    return SecurityLogProfileResult(
        full_name=full, instance_path=path, created=created, module="AFM"
    )
