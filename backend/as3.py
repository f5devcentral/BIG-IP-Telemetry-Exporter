"""BIG-IP AS3 (appsvcs) install check and declaration deploy."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from backend.bigip_client import BigIPClient, BigIPError, format_icontrol_error
from backend.bigip_resource import is_not_found, path_from_self_link

AS3_INFO_PATH = "/mgmt/shared/appsvcs/info"
AS3_DECLARE_PATH = "/mgmt/shared/appsvcs/declare"
PACKAGE_TASKS_PATH = "/mgmt/shared/iapp/package-management-tasks"
DEFAULT_SCHEMA_VERSION = "3.49.0"
DEFAULT_INSTALL_TIMEOUT_SEC = 600
DEFAULT_PACKAGE_POLL_SEC = 2.0


def _schema_version() -> str:
    return os.environ.get("BIGIP_AS3_SCHEMA_VERSION", DEFAULT_SCHEMA_VERSION).strip()


def _rpm_path() -> str:
    return os.environ.get("BIGIP_AS3_RPM_PATH", "").strip()


def _auto_install_enabled() -> bool:
    return os.environ.get("BIGIP_AS3_AUTO_INSTALL", "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _install_timeout_sec() -> int:
    raw = os.environ.get("BIGIP_AS3_INSTALL_TIMEOUT_SEC", str(DEFAULT_INSTALL_TIMEOUT_SEC)).strip()
    return int(raw)


def as3_info(client: BigIPClient) -> dict[str, Any] | None:
    """Return AS3 info document when appsvcs is installed, else None."""
    try:
        data = client.get(AS3_INFO_PATH)
    except BigIPError as exc:
        if is_not_found(exc) or "404" in str(exc):
            return None
        raise
    return data if isinstance(data, dict) else None


def _wait_package_task(client: BigIPClient, task: dict[str, Any]) -> None:
    link = path_from_self_link(str(task.get("selfLink", "")))
    if not link:
        raise BigIPError("AS3 package install task missing selfLink")
    deadline = time.time() + _install_timeout_sec()
    while time.time() < deadline:
        state = client.get(link)
        if not isinstance(state, dict):
            raise BigIPError("Invalid package task status response")
        status = str(state.get("status", "")).upper()
        if status == "FINISHED":
            err = state.get("error")
            if err:
                raise BigIPError(f"AS3 package install failed: {err}")
            return
        if status in ("FAILED", "ERROR", "CANCELLED"):
            raise BigIPError(
                f"AS3 package install {status.lower()}: "
                f"{state.get('error') or state.get('message') or state}",
            )
        time.sleep(DEFAULT_PACKAGE_POLL_SEC)
    raise BigIPError("Timed out waiting for AS3 RPM installation")


def install_as3_rpm(client: BigIPClient, rpm_file: Path) -> None:
    """Upload and install an f5-appsvcs RPM on the BIG-IP."""
    if not rpm_file.is_file():
        raise BigIPError(f"AS3 RPM not found: {rpm_file}")
    name = rpm_file.name
    client.upload_bytes(f"/mgmt/shared/file-transfer/uploads/{name}", rpm_file.read_bytes())
    task = client.post(
        PACKAGE_TASKS_PATH,
        json_body={
            "operation": "INSTALL",
            "packageFilePath": f"/var/config/rest/downloads/{name}",
        },
    )
    if not isinstance(task, dict):
        raise BigIPError("Unexpected response from package-management-tasks")
    _wait_package_task(client, task)
    if as3_info(client) is None:
        raise BigIPError("AS3 RPM install finished but /mgmt/shared/appsvcs/info is unavailable")


def ensure_as3_available(client: BigIPClient) -> dict[str, Any]:
    """Verify AS3 is installed; optionally install from BIGIP_AS3_RPM_PATH."""
    info = as3_info(client)
    if info is not None:
        return info
    if not _auto_install_enabled():
        raise BigIPError(
            "F5 Application Services (AS3) is not installed on this BIG-IP. "
            "Install the f5-appsvcs RPM or set BIGIP_AS3_AUTO_INSTALL=true with BIGIP_AS3_RPM_PATH."
        )
    rpm = _rpm_path()
    if not rpm:
        raise BigIPError(
            "AS3 is not installed. Set BIGIP_AS3_RPM_PATH to a local f5-appsvcs-*.noarch.rpm "
            "file so the exporter can upload and install it, or install AS3 on the BIG-IP manually."
        )
    install_as3_rpm(client, Path(rpm))
    info = as3_info(client)
    if info is None:
        raise BigIPError("AS3 is still unavailable after RPM installation")
    return info


def _declaration_failed(response: dict[str, Any]) -> bool:
    if response.get("message") in ("declaration is invalid", "declaration failed"):
        return True
    errors = response.get("errors")
    if isinstance(errors, list) and errors:
        return True
    results = response.get("results")
    if isinstance(results, list):
        for entry in results:
            if isinstance(entry, dict) and int(entry.get("code", 200)) >= 400:
                return True
    return False


def post_declaration(client: BigIPClient, declaration: dict[str, Any]) -> dict[str, Any]:
    """POST an AS3 declaration (deploy)."""
    response = client.post(AS3_DECLARE_PATH, json_body=declaration)
    if not isinstance(response, dict):
        return {}
    if _declaration_failed(response):
        raise BigIPError(format_icontrol_error(422, json.dumps(response)))
    return response


def schema_version_for_declaration(client: BigIPClient) -> str:
    info = as3_info(client)
    if info:
        for key in ("schemaCurrent", "schemaVersion"):
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return _schema_version()
