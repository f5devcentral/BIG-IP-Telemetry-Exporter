"""Ensure an LTM request/response logging profile exists for OTLP log shipping."""

from __future__ import annotations

import os
from dataclasses import dataclass

from backend.bigip_client import BigIPClient
from backend.bigip_resource import ensure_config_object, is_not_found
from backend.log_templates import REQUEST_EVENT_TEMPLATE, RESPONSE_EVENT_TEMPLATE

DEFAULT_PROFILE_NAME = "bigip-telemetry-requestlog"
DEFAULT_PARTITION = "Common"
PROFILE_COLLECTION = "/mgmt/tm/ltm/profile/request-log"
PROFILE_DESCRIPTION = (
    "Created by BIG-IP Telemetry Exporter. Attach to virtual servers as a Request Logging "
    "profile; request/response logs will be forwarded to the OpenTelemetry collector in a "
    "future release."
)
REQUEST_LOG_TEMPLATE = REQUEST_EVENT_TEMPLATE
RESPONSE_LOG_TEMPLATE = RESPONSE_EVENT_TEMPLATE


@dataclass(frozen=True)
class RequestLogProfileResult:
    full_name: str
    instance_path: str
    created: bool

    @property
    def attach_hint(self) -> str:
        return (
            f"On a virtual server, add Request Logging profile {self.full_name} "
            f"(iControl: profiles reference name {self.full_name})."
        )


def _auto_create_enabled() -> bool:
    return os.environ.get("BIGIP_REQUEST_LOG_AUTO_CREATE", "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def profile_name() -> str:
    return os.environ.get("BIGIP_REQUEST_LOG_PROFILE_NAME", DEFAULT_PROFILE_NAME).strip()


def profile_partition() -> str:
    return (
        os.environ.get("BIGIP_LOG_PROFILE_PARTITION")
        or os.environ.get("BIGIP_REQUEST_LOG_PARTITION", DEFAULT_PARTITION)
    ).strip() or "Common"


def profile_instance_path(*, partition: str | None = None, name: str | None = None) -> str:
    part = partition or profile_partition()
    prof = name or profile_name()
    return f"{PROFILE_COLLECTION}/~{part}~{prof}"


def profile_full_name(*, partition: str | None = None, name: str | None = None) -> str:
    part = partition or profile_partition()
    prof = name or profile_name()
    return f"/{part}/{prof}"


def _profile_settings() -> dict[str, str]:
    return {
        "description": PROFILE_DESCRIPTION,
        "requestLogging": "enabled",
        "responseLogging": "enabled",
        "requestLogTemplate": REQUEST_LOG_TEMPLATE,
        "responseLogTemplate": RESPONSE_LOG_TEMPLATE,
    }


def _desired_profile_body(*, partition: str, name: str) -> dict[str, str]:
    return {
        "name": name,
        "partition": partition,
        **_profile_settings(),
    }


def ensure_request_log_profile(
    client: BigIPClient,
    *,
    partition: str | None = None,
    name: str | None = None,
) -> RequestLogProfileResult:
    """Create or update the exporter-managed request-log profile on BIG-IP."""
    if not _auto_create_enabled():
        full = profile_full_name(partition=partition, name=name)
        return RequestLogProfileResult(
            full_name=full,
            instance_path=profile_instance_path(partition=partition, name=name),
            created=False,
        )

    part = partition or profile_partition()
    prof = name or profile_name()
    path = profile_instance_path(partition=part, name=prof)
    full = profile_full_name(partition=part, name=prof)
    created = ensure_config_object(
        client,
        collection_path=PROFILE_COLLECTION,
        instance_path=path,
        create_body=_desired_profile_body(partition=part, name=prof),
        patch_body=_profile_settings(),
    )
    return RequestLogProfileResult(full_name=full, instance_path=path, created=created)
