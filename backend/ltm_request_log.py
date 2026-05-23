"""Ensure an LTM request/response logging profile exists for OTLP log shipping."""

from __future__ import annotations

import os
from dataclasses import dataclass

from backend.bigip_client import BigIPClient, BigIPError

DEFAULT_PROFILE_NAME = "bigip-metrics-requestlog"
DEFAULT_PARTITION = "Common"
PROFILE_COLLECTION = "/mgmt/tm/ltm/profile/request-log"
PROFILE_DESCRIPTION = (
    "Created by BIG-IP Metrics Exporter. Attach to virtual servers as a Request Logging "
    "profile; request/response logs will be forwarded to the OpenTelemetry collector in a "
    "future release."
)


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
    return os.environ.get("BIGIP_REQUEST_LOG_PARTITION", DEFAULT_PARTITION).strip() or "Common"


def profile_instance_path(*, partition: str | None = None, name: str | None = None) -> str:
    part = partition or profile_partition()
    prof = name or profile_name()
    return f"{PROFILE_COLLECTION}/~{part}~{prof}"


def profile_full_name(*, partition: str | None = None, name: str | None = None) -> str:
    part = partition or profile_partition()
    prof = name or profile_name()
    return f"/{part}/{prof}"


def _desired_profile_body(*, partition: str, name: str) -> dict[str, str]:
    return {
        "name": name,
        "partition": partition,
        "description": PROFILE_DESCRIPTION,
        "requestLogging": "enabled",
        "responseLogging": "enabled",
    }


def _is_not_found(exc: BigIPError) -> bool:
    return "404" in str(exc)


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
    desired = _desired_profile_body(partition=part, name=prof)

    try:
        client.get(path)
    except BigIPError as exc:
        if not _is_not_found(exc):
            raise
        client.post(PROFILE_COLLECTION, json_body=desired)
        return RequestLogProfileResult(full_name=full, instance_path=path, created=True)

    client.patch(
        path,
        json_body={
            "description": desired["description"],
            "requestLogging": desired["requestLogging"],
            "responseLogging": desired["responseLogging"],
        },
    )
    return RequestLogProfileResult(full_name=full, instance_path=path, created=False)
