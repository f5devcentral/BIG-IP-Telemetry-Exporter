"""ASM and AFM logging profiles (ASM via /mgmt/tm/asm/logging-profiles, AFM via security log profile)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from backend.bigip_client import BigIPClient, BigIPError
from backend.bigip_resource import (
    ensure_config_object,
    find_collection_item,
    is_invalid_path,
    path_from_self_link,
)
from backend.module_provision import is_module_provisioned

# Re-export for tests and documentation.
__all__ = [
    "SecurityLogProfileResult",
    "ensure_afm_log_profile",
    "ensure_asm_log_profile",
]

SECURITY_LOG_PROFILE_COLLECTION = "/mgmt/tm/security/log/profile"
ASM_LOG_PROFILE_COLLECTION = "/mgmt/tm/asm/logging-profiles"
DEFAULT_PARTITION = "Common"
DEFAULT_ASM_NAME = "bigip-metrics-asm-log"
DEFAULT_AFM_NAME = "bigip-metrics-afm-log"
DEFAULT_AFM_LOG_PUBLISHER = "/Common/local-db-publisher"
DEFAULT_AFM_AGGREGATE_RATE_LIMIT = 1000
ASM_DESCRIPTION = (
    "Created by BIG-IP Metrics Exporter. Attach as an ASM Logging Profile on virtual servers "
    "or ASM policies; logs all requests locally (requestType all) for future OTLP export."
)
AFM_DESCRIPTION = (
    "Created by BIG-IP Metrics Exporter. Attach as a Security Log Profile on virtual servers "
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


def _asm_log_profile_path_candidates(partition: str, name: str) -> list[str]:
    """ASM logging-profiles may not support ~Partition~name GET; try common URI forms."""
    return [
        f"{ASM_LOG_PROFILE_COLLECTION}/{name}",
        f"{ASM_LOG_PROFILE_COLLECTION}/~{partition}~{name}",
    ]


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


def _asm_application_settings() -> dict:
    """Matches /mgmt/tm/asm/logging-profiles application object (requestType all)."""
    return {
        "localStorage": {"enabled": True},
        "remoteStorage": {"enabled": False},
        "requestType": "all",
    }


def _asm_profile_settings(*, partition: str, name: str) -> dict:
    return {
        "name": name,
        "partition": partition,
        "description": ASM_DESCRIPTION,
        "application": _asm_application_settings(),
    }


def _asm_patch_settings() -> dict:
    return {
        "description": ASM_DESCRIPTION,
        "application": _asm_application_settings(),
    }


def _afm_network_settings() -> dict:
    """Matches POST /mgmt/tm/security/log/profile network object."""
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


def _patch_asm_logging_profile(
    client: BigIPClient,
    *,
    partition: str,
    name: str,
    existing: dict | None,
) -> str:
    """PATCH an existing ASM logging profile; return the REST path that worked."""
    patch_body = _asm_patch_settings()
    candidates: list[str] = []
    if existing:
        link_path = path_from_self_link(str(existing.get("selfLink", "")))
        if link_path:
            candidates.append(link_path)
    candidates.extend(_asm_log_profile_path_candidates(partition, name))

    seen: set[str] = set()
    last_exc: BigIPError | None = None
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        try:
            client.patch(path, json_body=patch_body)
            return path
        except BigIPError as exc:
            last_exc = exc
            if is_invalid_path(exc):
                continue
            raise
    if last_exc:
        raise last_exc
    raise BigIPError("No valid ASM logging profile path for PATCH")


def ensure_asm_log_profile(
    client: BigIPClient,
    *,
    partition: str | None = None,
    name: str | None = None,
) -> SecurityLogProfileResult | None:
    """ASM logging profile via /mgmt/tm/asm/logging-profiles (requestType all)."""
    if not is_module_provisioned(client, "asm"):
        return None
    part = partition or _partition()
    prof = name or _asm_name()
    full = _full_name(part, prof)
    display_path = f"{ASM_LOG_PROFILE_COLLECTION}/{prof}"
    if not _asm_auto_create():
        return SecurityLogProfileResult(
            full_name=full,
            instance_path=display_path,
            created=False,
            module="ASM",
        )

    create_body = _asm_profile_settings(partition=part, name=prof)
    existing: dict | None = None
    try:
        existing = find_collection_item(
            client,
            ASM_LOG_PROFILE_COLLECTION,
            name=prof,
            partition=part,
        )
    except BigIPError as exc:
        if not is_invalid_path(exc):
            raise

    if existing:
        path = _patch_asm_logging_profile(
            client, partition=part, name=prof, existing=existing
        )
        return SecurityLogProfileResult(
            full_name=full, instance_path=path, created=False, module="ASM"
        )

    try:
        client.post(ASM_LOG_PROFILE_COLLECTION, json_body=create_body)
        return SecurityLogProfileResult(
            full_name=full,
            instance_path=display_path,
            created=True,
            module="ASM",
        )
    except BigIPError as post_exc:
        try:
            existing = find_collection_item(
                client,
                ASM_LOG_PROFILE_COLLECTION,
                name=prof,
                partition=part,
            )
        except BigIPError:
            raise post_exc from None
        if not existing:
            raise post_exc from None
        path = _patch_asm_logging_profile(
            client, partition=part, name=prof, existing=existing
        )
        return SecurityLogProfileResult(
            full_name=full, instance_path=path, created=False, module="ASM"
        )


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
