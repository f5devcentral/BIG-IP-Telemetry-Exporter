"""Provision exporter logging and analytics profiles via AS3 declarations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from backend.as3 import ensure_as3_available, post_declaration, schema_version_for_declaration
from backend.bigip_client import BigIPClient, BigIPError
from backend.log_forwarding import (
    LOG_HSL_DEST_NAME,
    LOG_HSL_POOL_NAME,
    LOG_POOL_NAME,
    LOG_PUBLISHER_NAME,
    LOG_SYSLOG_DEST_NAME,
    hsl_port,
    syslog_host,
    syslog_port,
    syslog_target,
    hsl_target,
)
from backend.log_templates import REQUEST_EVENT_TEMPLATE, RESPONSE_EVENT_TEMPLATE
from backend.module_provision import is_module_provisioned

DEFAULT_PARTITION = "Common"
DEFAULT_TENANT = "Common"
# AS3 Common tenant only allows a child application named "Shared".
DEFAULT_APPLICATION = "Shared"
DEFAULT_DECLARATION_ID = "bigip_telemetry_exporter_log_profiles"

DEFAULT_REQUEST_LOG_NAME = "bigip-telemetry-requestlog"
DEFAULT_ASM_LOG_NAME = "bigip-telemetry-asm-log"
DEFAULT_AFM_LOG_NAME = "bigip-telemetry-afm-log"
DEFAULT_HTTP_ANALYTICS_NAME = "bigip-telemetry-http-analytics"
DEFAULT_TCP_ANALYTICS_NAME = "bigip-telemetry-tcp-analytics"

# Required for ASM remoteStorage "remote" (predefined format cannot have empty fields).
_ASM_REMOTE_LOG_FIELDS = [
    "date_time",
    "ip_client",
    "method",
    "uri",
    "response_code",
    "request_status",
    "support_id",
    "violations",
    "http_class_name",
    "unit_hostname",
]


@dataclass(frozen=True)
class LogProfilesResult:
    request_log_profile: str | None = None
    request_log_profile_created: bool | None = None
    asm_log_profile: str | None = None
    asm_log_profile_created: bool | None = None
    afm_log_profile: str | None = None
    afm_log_profile_created: bool | None = None
    http_analytics_profile: str | None = None
    http_analytics_profile_created: bool | None = None
    tcp_analytics_profile: str | None = None
    tcp_analytics_profile_created: bool | None = None
    log_syslog_target: str | None = None
    log_hsl_target: str | None = None


def _partition() -> str:
    return os.environ.get("BIGIP_LOG_PROFILE_PARTITION", DEFAULT_PARTITION).strip() or "Common"


def _tenant() -> str:
    return os.environ.get("BIGIP_AS3_TENANT", DEFAULT_TENANT).strip() or DEFAULT_TENANT


def _application_key(tenant_name: str) -> str:
    """Return the Application key under a tenant (Common requires Shared)."""
    if tenant_name == "Common":
        return "Shared"
    return os.environ.get("BIGIP_AS3_APPLICATION", "bigip_telemetry_exporter").strip()


def _declaration_id() -> str:
    return os.environ.get("BIGIP_AS3_DECLARATION_ID", DEFAULT_DECLARATION_ID).strip()


def _request_log_name() -> str:
    return os.environ.get("BIGIP_REQUEST_LOG_PROFILE_NAME", DEFAULT_REQUEST_LOG_NAME).strip()


def _asm_name() -> str:
    return os.environ.get("BIGIP_ASM_LOG_PROFILE_NAME", DEFAULT_ASM_LOG_NAME).strip()


def _afm_name() -> str:
    return os.environ.get("BIGIP_AFM_LOG_PROFILE_NAME", DEFAULT_AFM_LOG_NAME).strip()


def _http_analytics_name() -> str:
    return os.environ.get("BIGIP_HTTP_ANALYTICS_PROFILE_NAME", DEFAULT_HTTP_ANALYTICS_NAME).strip()


def _tcp_analytics_name() -> str:
    return os.environ.get("BIGIP_TCP_ANALYTICS_PROFILE_NAME", DEFAULT_TCP_ANALYTICS_NAME).strip()


def _full_name(partition: str, name: str) -> str:
    return f"/{partition}/{name}"


def _auto_create(env_key: str, *, default: bool = True) -> bool:
    default_str = "true" if default else "false"
    return os.environ.get(env_key, default_str).strip().lower() not in ("0", "false", "no")


def _ltm_enabled() -> bool:
    return _auto_create("BIGIP_REQUEST_LOG_AUTO_CREATE")


def _asm_enabled() -> bool:
    return _auto_create("BIGIP_ASM_LOG_AUTO_CREATE")


def _afm_enabled() -> bool:
    return _auto_create("BIGIP_AFM_LOG_AUTO_CREATE")


def _http_analytics_enabled() -> bool:
    return _auto_create("BIGIP_HTTP_ANALYTICS_AUTO_CREATE")


def _tcp_analytics_enabled() -> bool:
    return _auto_create("BIGIP_TCP_ANALYTICS_AUTO_CREATE")


def _pool_member(host: str, port: int) -> dict[str, Any]:
    return {
        "serverAddresses": [host],
        "servicePort": port,
    }


def _build_remote_log_infrastructure(
    host: str,
    *,
    include_syslog: bool,
    include_hsl: bool,
) -> dict[str, Any]:
    """AS3 pool / log destination / publisher objects for remote log forwarding."""
    objects: dict[str, Any] = {}

    if include_syslog:
        objects[LOG_POOL_NAME] = {
            "class": "Pool",
            "members": [_pool_member(host, syslog_port())],
        }
        objects[LOG_HSL_DEST_NAME] = {
            "class": "Log_Destination",
            "type": "remote-high-speed-log",
            "pool": {"use": LOG_POOL_NAME},
            "protocol": "tcp",
        }
        objects[LOG_SYSLOG_DEST_NAME] = {
            "class": "Log_Destination",
            "type": "remote-syslog",
            "format": "rfc5424",
            "remoteHighSpeedLog": {"use": LOG_HSL_DEST_NAME},
        }
        objects[LOG_PUBLISHER_NAME] = {
            "class": "Log_Publisher",
            "destinations": [{"use": LOG_SYSLOG_DEST_NAME}],
        }

    if include_hsl:
        objects[LOG_HSL_POOL_NAME] = {
            "class": "Pool",
            "members": [_pool_member(host, hsl_port())],
        }

    return objects


def _traffic_log_profile() -> dict[str, Any]:
    """LTM request/response logging forwarded via HSL to the collector tcplog port."""
    return {
        "class": "Traffic_Log_Profile",
        "requestSettings": {
            "requestEnabled": True,
            "requestTemplate": REQUEST_EVENT_TEMPLATE,
            "requestPool": {"use": LOG_HSL_POOL_NAME},
            "requestProtocol": "mds-tcp",
        },
        "responseSettings": {
            "responseEnabled": True,
            "responseTemplate": RESPONSE_EVENT_TEMPLATE,
            "responsePool": {"use": LOG_HSL_POOL_NAME},
            "responseProtocol": "mds-tcp",
        },
    }


def _asm_security_log_profile(host: str) -> dict[str, Any]:
    """ASM application security logging — remote reporting server to collector syslog port."""
    return {
        "class": "Security_Log_Profile",
        "application": {
            "localStorage": False,
            "remoteStorage": "remote",
            "protocol": "tcp",
            "servers": [
                {
                    "address": host,
                    "port": str(syslog_port()),
                },
            ],
            "storageFilter": {
                "requestType": "all",
            },
            "responseLogging": "all",
            "storageFormat": {
                "delimiter": ",",
                "fields": _ASM_REMOTE_LOG_FIELDS,
            },
        },
    }


def _afm_security_log_profile() -> dict[str, Any]:
    """AFM network firewall logging — remote syslog via shared log publisher."""
    return {
        "class": "Security_Log_Profile",
        "network": {
            "publisher": {"use": LOG_PUBLISHER_NAME},
            "logRuleMatchAccepts": True,
            "logRuleMatchDrops": True,
            "logRuleMatchRejects": True,
            "logIpErrors": True,
            "logTcpErrors": True,
            "logTcpEvents": True,
            "logTranslationFields": True,
        },
    }


def _http_analytics_profile() -> dict[str, Any]:
    return {
        "class": "Analytics_Profile",
        "collectGeo": True,
        "collectMaxTpsAndThroughput": True,
        "collectOsAndBrowser": True,
        "collectIp": True,
        "collectMethod": True,
        "collectPageLoadTime": True,
        "collectResponseCode": True,
        "collectSubnet": True,
        "collectUrl": True,
        "collectUserAgent": True,
        "collectUserSession": True,
        "publishIruleStatistics": True,
    }


def _tcp_analytics_profile() -> dict[str, Any]:
    return {
        "class": "Analytics_TCP_Profile",
        "collectCity": True,
        "collectContinent": True,
        "collectCountry": True,
        "collectNexthop": True,
        "collectPostCode": True,
        "collectRegion": True,
        "collectRemoteHostIp": True,
        "collectRemoteHostSubnet": True,
        "collectedByServerSide": True,
    }


def build_log_profiles_declaration(
    client: BigIPClient,
    *,
    log_host: str,
    include_ltm: bool = True,
    include_asm: bool = False,
    include_afm: bool = False,
    include_http_analytics: bool = False,
    include_tcp_analytics: bool = False,
) -> dict[str, Any]:
    """Build an AS3 ADC declaration containing remote logging/analytics profiles."""
    app_objects: dict[str, Any] = {}

    need_syslog_chain = include_afm
    need_hsl = include_ltm and _ltm_enabled()
    if need_syslog_chain or need_hsl:
        app_objects.update(
            _build_remote_log_infrastructure(
                log_host,
                include_syslog=need_syslog_chain,
                include_hsl=need_hsl,
            ),
        )

    if include_ltm and _ltm_enabled():
        app_objects[_request_log_name()] = _traffic_log_profile()

    if include_asm and _asm_enabled():
        app_objects[_asm_name()] = _asm_security_log_profile(log_host)

    if include_afm and _afm_enabled():
        app_objects[_afm_name()] = _afm_security_log_profile()

    if include_http_analytics and _http_analytics_enabled():
        app_objects[_http_analytics_name()] = _http_analytics_profile()

    if include_tcp_analytics and _tcp_analytics_enabled():
        app_objects[_tcp_analytics_name()] = _tcp_analytics_profile()

    tenant_name = _tenant()
    app_key = _application_key(tenant_name)
    return {
        "class": "ADC",
        "schemaVersion": schema_version_for_declaration(client),
        "id": _declaration_id(),
        "remark": "BIG-IP Telemetry Exporter remote log and AVR profiles",
        tenant_name: {
            "class": "Tenant",
            app_key: {
                "class": "Application",
                "template": "shared",
                **app_objects,
            },
        },
    }


def _provision_flags(client: BigIPClient) -> dict[str, bool]:
    return {
        "ltm": is_module_provisioned(client, "ltm"),
        "asm": is_module_provisioned(client, "asm"),
        "afm": is_module_provisioned(client, "afm"),
        "avr": is_module_provisioned(client, "avr"),
    }


def ensure_log_profiles_via_as3(
    client: BigIPClient,
    *,
    log_host: str | None = None,
) -> LogProfilesResult:
    """Install AS3 if needed, then deploy remote logging/analytics profiles."""
    ensure_as3_available(client)
    flags = _provision_flags(client)
    part = _partition()
    host = (log_host or syslog_host()).strip()

    include_ltm = flags["ltm"] and _ltm_enabled()
    include_asm = flags["asm"] and _asm_enabled()
    include_afm = flags["afm"] and _afm_enabled()
    include_http = flags["avr"] and _http_analytics_enabled()
    include_tcp = flags["avr"] and _tcp_analytics_enabled()

    if not any((include_ltm, include_asm, include_afm, include_http, include_tcp)):
        return LogProfilesResult()

    declaration = build_log_profiles_declaration(
        client,
        log_host=host,
        include_ltm=include_ltm,
        include_asm=include_asm,
        include_afm=include_afm,
        include_http_analytics=include_http,
        include_tcp_analytics=include_tcp,
    )
    post_declaration(client, declaration)

    return LogProfilesResult(
        request_log_profile=_full_name(part, _request_log_name()) if include_ltm else None,
        request_log_profile_created=None,
        asm_log_profile=_full_name(part, _asm_name()) if include_asm else None,
        asm_log_profile_created=None,
        afm_log_profile=_full_name(part, _afm_name()) if include_afm else None,
        afm_log_profile_created=None,
        http_analytics_profile=_full_name(part, _http_analytics_name()) if include_http else None,
        http_analytics_profile_created=None,
        tcp_analytics_profile=_full_name(part, _tcp_analytics_name()) if include_tcp else None,
        tcp_analytics_profile_created=None,
        log_syslog_target=syslog_target() if include_asm or include_afm else None,
        log_hsl_target=hsl_target() if include_ltm else None,
    )
