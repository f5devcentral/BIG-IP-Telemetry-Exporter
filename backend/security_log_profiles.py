"""Legacy REST helpers (superseded by backend.as3_log_profiles on connect)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from backend.bigip_client import BigIPClient
from backend.bigip_resource import ensure_config_object
from backend.module_provision import is_module_provisioned

# Re-export for tests and documentation.
__all__ = [
    "SecurityLogProfileResult",
    "ensure_afm_log_profile",
    "ensure_asm_log_profile",
]

SECURITY_LOG_PROFILE_COLLECTION = "/mgmt/tm/security/log/profile"
DEFAULT_PARTITION = "Common"
DEFAULT_ASM_NAME = "bigip-telemetry-asm-log"
DEFAULT_AFM_NAME = "bigip-telemetry-afm-log"
DEFAULT_AFM_LOG_PUBLISHER = "/Common/local-db-publisher"
DEFAULT_AFM_AGGREGATE_RATE_LIMIT = 1000
AFM_DESCRIPTION = (
    "Created by BIG-IP Telemetry Exporter. Attach as a Security Log Profile on virtual servers "
    "for AFM (Network Firewall); logs ACL matches and network events for future OTLP export."
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


def _afm_log_publisher() -> str:
    return os.environ.get("BIGIP_AFM_LOG_PUBLISHER", DEFAULT_AFM_LOG_PUBLISHER).strip()


def _afm_aggregate_rate_limit() -> int:
    raw = os.environ.get(
        "BIGIP_AFM_AGGREGATE_RATE_LIMIT",
        str(DEFAULT_AFM_AGGREGATE_RATE_LIMIT),
    ).strip()
    return int(raw)


def _full_name(partition: str, name: str) -> str:
    return f"/{partition}/{name}"


def _security_log_profile_path(partition: str, name: str) -> str:
    return f"{SECURITY_LOG_PROFILE_COLLECTION}/~{partition}~{name}"


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


def _asm_profile_settings(*, partition: str, name: str) -> dict:
    """POST /mgmt/tm/security/log/profile — name and partition only."""
    return {"name": name, "partition": partition}


def ensure_asm_log_profile(
    client: BigIPClient,
    *,
    partition: str | None = None,
    name: str | None = None,
) -> SecurityLogProfileResult | None:
    """ASM security log profile shell via POST /mgmt/tm/security/log/profile."""
    if not is_module_provisioned(client, "asm"):
        return None
    part = partition or _partition()
    prof = name or _asm_name()
    path = _security_log_profile_path(part, prof)
    full = _full_name(part, prof)
    body = _asm_profile_settings(partition=part, name=prof)
    if not _asm_auto_create():
        return SecurityLogProfileResult(
            full_name=full,
            instance_path=path,
            created=False,
            module="ASM",
        )

    created = ensure_config_object(
        client,
        collection_path=SECURITY_LOG_PROFILE_COLLECTION,
        instance_path=path,
        create_body=body,
        patch_body=body,
    )
    return SecurityLogProfileResult(
        full_name=full, instance_path=path, created=created, module="ASM"
    )


def _afm_network_settings() -> dict:
    """Network sub-profile for POST /mgmt/tm/security/log/profile."""
    return {
        "logPublisher": _afm_log_publisher(),
        "logRuleMatches": ["accept", "drop", "reject"],
        "logIpErrors": "enabled",
        "logTcpErrors": "enabled",
        "logTcpEvents": "enabled",
        "logTranslationFields": "enabled",
        "aggregateRateLimit": _afm_aggregate_rate_limit(),
    }


def _afm_profile_settings(*, partition: str, name: str) -> dict:
    return {
        "name": name,
        "partition": partition,
        "description": AFM_DESCRIPTION,
        "network": _afm_network_settings(),
    }


def _afm_patch_settings() -> dict:
    return {
        "description": AFM_DESCRIPTION,
        "network": _afm_network_settings(),
    }


def ensure_afm_log_profile(
    client: BigIPClient,
    *,
    partition: str | None = None,
    name: str | None = None,
) -> SecurityLogProfileResult | None:
    """AFM security log profile via /mgmt/tm/security/log/profile (network firewall logging)."""
    if not is_module_provisioned(client, "afm"):
        return None
    part = partition or _partition()
    prof = name or _afm_name()
    full = _full_name(part, prof)
    path = _security_log_profile_path(part, prof)
    if not _afm_auto_create():
        return SecurityLogProfileResult(
            full_name=full,
            instance_path=path,
            created=False,
            module="AFM",
        )

    created = ensure_config_object(
        client,
        collection_path=SECURITY_LOG_PROFILE_COLLECTION,
        instance_path=path,
        create_body=_afm_profile_settings(partition=part, name=prof),
        patch_body=_afm_patch_settings(),
    )
    return SecurityLogProfileResult(
        full_name=full, instance_path=path, created=created, module="AFM"
    )
