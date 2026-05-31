"""Reload or restart Prometheus for validation (docker compose, kubectl, or HTTP reload)."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

from backend.bigip_client import BigIPError

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
PROMETHEUS_SERVICE = os.environ.get("PROMETHEUS_COMPOSE_SERVICE", "prometheus")
PROMETHEUS_K8S_DEPLOYMENT = os.environ.get("PROMETHEUS_K8S_DEPLOYMENT", "deployment/prometheus")


def _prometheus_base_url(request_host: str | None = None) -> str:
    if url := os.environ.get("PROMETHEUS_RELOAD_URL", "").strip():
        return url.rstrip("/")
    if url := os.environ.get("PROMETHEUS_UI_URL", "").strip():
        return url.rstrip("/")
    host = (request_host or "127.0.0.1").split(":")[0]
    port = os.environ.get("PROMETHEUS_BROWSER_PORT", "9090")
    return f"http://{host}:{port}"


def _reload_wipes_tsdb() -> bool:
    return os.environ.get("PROMETHEUS_RELOAD_WIPE_TSDB", "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _wait_prometheus_ready(base: str, *, timeout_sec: float = 120.0) -> None:
    deadline = time.time() + timeout_sec
    last_error = ""
    while time.time() < deadline:
        try:
            r = requests.get(f"{base}/-/ready", timeout=5)
            if r.status_code == 200:
                return
            last_error = f"status {r.status_code}"
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(2)
    raise BigIPError(
        f"Prometheus did not become ready at {base} within {int(timeout_sec)}s ({last_error})",
    )


def _post_reload(base: str) -> dict[str, Any]:
    url = f"{base}/-/reload"
    try:
        r = requests.post(url, timeout=30)
    except requests.RequestException as exc:
        raise BigIPError(
            f"Cannot reach Prometheus at {base} for reload ({exc}). "
            "Ensure Prometheus is running with --web.enable-lifecycle.",
        ) from exc
    if r.status_code == 200:
        return {"reload_url": url}
    if r.status_code == 404:
        raise BigIPError(
            "Prometheus lifecycle API disabled. Add --web.enable-lifecycle to Prometheus.",
        )
    raise BigIPError(f"Prometheus reload failed ({r.status_code}): {r.text[:300]}")


def _detect_restart_mode() -> str:
    explicit = os.environ.get("PROMETHEUS_RESTART_MODE", "").strip().lower()
    if explicit in ("docker", "kubernetes", "none"):
        return explicit
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return "kubernetes"
    if shutil.which("docker") and COMPOSE_FILE.is_file():
        return "docker"
    if shutil.which("kubectl"):
        return "kubernetes"
    return "none"


def restart_hint(mode: str) -> str:
    ns = os.environ.get("PROMETHEUS_K8S_NAMESPACE", "bigip-telemetry")
    if mode == "docker":
        return (
            f"docker compose -f {COMPOSE_FILE} stop {PROMETHEUS_SERVICE} && "
            f"docker compose -f {COMPOSE_FILE} rm -f {PROMETHEUS_SERVICE} && "
            f"docker compose -f {COMPOSE_FILE} up -d {PROMETHEUS_SERVICE}"
        )
    if mode == "kubernetes":
        return f"kubectl -n {ns} rollout restart {PROMETHEUS_K8S_DEPLOYMENT}"
    return "Reload via API or restart Prometheus manually."


def _wipe_tsdb_docker() -> dict[str, Any]:
    if not COMPOSE_FILE.is_file():
        raise BigIPError(f"docker-compose.yml not found at {COMPOSE_FILE}")
    if not shutil.which("docker"):
        raise BigIPError("docker not found in PATH")

    compose = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    steps = [
        (*compose, "stop", PROMETHEUS_SERVICE),
        (*compose, "rm", "-f", PROMETHEUS_SERVICE),
        (*compose, "up", "-d", PROMETHEUS_SERVICE),
    ]
    commands: list[str] = []
    for cmd in steps:
        result = _run_restart_command(cmd, cwd=str(REPO_ROOT), label="docker compose")
        commands.append(result["command"])

    return {
        "wipe_mode": "docker",
        "commands": commands,
        "message": "Prometheus container recreated (TSDB directory cleared).",
    }


def _wipe_tsdb_kubernetes() -> dict[str, Any]:
    if not shutil.which("kubectl"):
        raise BigIPError(
            "kubectl not found. From a machine with cluster access run: "
            f"{restart_hint('kubernetes')}",
        )
    ns = os.environ.get("PROMETHEUS_K8S_NAMESPACE", "bigip-telemetry")
    cmd = [
        "kubectl",
        "rollout",
        "restart",
        PROMETHEUS_K8S_DEPLOYMENT,
        "-n",
        ns,
    ]
    result = _run_restart_command(cmd, label="kubectl")
    return {
        "wipe_mode": "kubernetes",
        "commands": [result["command"]],
        "message": "Prometheus pod restarted (emptyDir TSDB volume cleared).",
    }


def _wipe_tsdb_http(base: str) -> dict[str, Any]:
    """Delete all series via Prometheus admin API (no container restart)."""
    delete_url = f"{base}/api/v1/admin/tsdb/delete_series"
    try:
        r = requests.post(
            delete_url,
            params={"match[]": '{__name__=~".+"}'},
            timeout=120,
        )
    except requests.RequestException as exc:
        raise BigIPError(f"Cannot reach Prometheus at {base} to wipe TSDB ({exc})") from exc
    if r.status_code != 200:
        raise BigIPError(f"TSDB delete_series failed ({r.status_code}): {r.text[:300]}")

    tomb_url = f"{base}/api/v1/admin/tsdb/clean_tombstones"
    try:
        requests.post(tomb_url, timeout=120)
    except requests.RequestException:
        pass  # optional cleanup step

    return {
        "wipe_mode": "http_admin",
        "commands": [delete_url],
        "message": "All Prometheus time series deleted via admin API (blocks compact over time).",
    }


def _wipe_tsdb_for_reload(*, request_host: str | None = None) -> dict[str, Any]:
    if cmd := os.environ.get("PROMETHEUS_RELOAD_WIPE_CMD", "").strip():
        result = _run_restart_command(shlex.split(cmd), label="PROMETHEUS_RELOAD_WIPE_CMD")
        return {
            "wipe_mode": "custom",
            "commands": [result["command"]],
            "message": "Prometheus TSDB wipe command completed.",
        }

    mode = _detect_restart_mode()
    if mode == "docker" and shutil.which("docker") and COMPOSE_FILE.is_file():
        return _wipe_tsdb_docker()
    if mode == "kubernetes" and shutil.which("kubectl"):
        return _wipe_tsdb_kubernetes()

    return _wipe_tsdb_http(_prometheus_base_url(request_host))


def reload_prometheus(*, request_host: str | None = None) -> dict[str, Any]:
    """
    Wipe Prometheus TSDB (when enabled), wait for readiness, then POST /-/reload.
    """
    base = _prometheus_base_url(request_host)
    wipe_info: dict[str, Any] | None = None

    if _reload_wipes_tsdb():
        wipe_info = _wipe_tsdb_for_reload(request_host=request_host)
        if wipe_info.get("wipe_mode") in ("docker", "kubernetes", "custom"):
            _wait_prometheus_ready(base)
    else:
        # Config-only reload without recreating storage
        reload_only = _post_reload(base)
        return {
            "ok": True,
            "action": "reload",
            "url": reload_only["reload_url"],
            "wipe_tsdb": False,
            "message": "Prometheus configuration reloaded (TSDB data retained).",
        }

    reload_result = _post_reload(base)
    message = "Prometheus TSDB wiped, service restarted, and configuration reloaded."
    if wipe_info and wipe_info.get("message"):
        message = f"{wipe_info['message']} Configuration reloaded."

    return {
        "ok": True,
        "action": "reload",
        "url": reload_result["reload_url"],
        "wipe_tsdb": True,
        "wipe": wipe_info,
        "message": message,
    }


def restart_prometheus() -> dict[str, Any]:
    """Restart the Prometheus process/container without an explicit config reload."""
    if cmd := os.environ.get("PROMETHEUS_RESTART_CMD", "").strip():
        return _run_restart_command(shlex.split(cmd), label="PROMETHEUS_RESTART_CMD")

    mode = _detect_restart_mode()
    if mode == "none":
        raise BigIPError(
            "Automatic Prometheus restart is not available in this environment. "
            f"Try Reload instead, or run manually: {restart_hint('docker')}",
        )

    if mode == "docker":
        wipe = _wipe_tsdb_docker()
        return {
            "ok": True,
            "action": "restart",
            "command": "; ".join(wipe.get("commands", [])),
            "message": wipe.get("message", "Prometheus restarted."),
            "wipe_tsdb": True,
        }

    wipe = _wipe_tsdb_kubernetes()
    return {
        "ok": True,
        "action": "restart",
        "command": "; ".join(wipe.get("commands", [])),
        "message": wipe.get("message", "Prometheus restarted."),
        "wipe_tsdb": True,
    }


def _run_restart_command(
    cmd: list[str],
    *,
    cwd: str | None = None,
    label: str,
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BigIPError(f"Prometheus operation timed out ({label})") from exc
    except OSError as exc:
        raise BigIPError(f"Prometheus operation failed to run ({label}): {exc}") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        raise BigIPError(
            f"Prometheus operation failed ({label}, exit {proc.returncode}): {detail}",
        )

    return {
        "ok": True,
        "command": " ".join(cmd),
        "stdout": (proc.stdout or "").strip()[:300],
    }


def control_status(*, request_host: str | None = None) -> dict[str, Any]:
    mode = _detect_restart_mode()
    wipe_on_reload = _reload_wipes_tsdb()
    restart_available = bool(os.environ.get("PROMETHEUS_RESTART_CMD"))
    wipe_available = bool(os.environ.get("PROMETHEUS_RELOAD_WIPE_CMD"))
    if mode == "docker" and shutil.which("docker") and COMPOSE_FILE.is_file():
        restart_available = True
        wipe_available = True
    if mode == "kubernetes" and shutil.which("kubectl"):
        restart_available = True
        wipe_available = True

    if wipe_on_reload:
        reload_hint = (
            "Wipes TSDB (recreates Docker/Kubernetes Prometheus or deletes all series via admin API), "
            "then reloads prometheus.yml and scrape targets. Set PROMETHEUS_RELOAD_WIPE_TSDB=false "
            "to reload config only."
        )
    else:
        reload_hint = "Reloads prometheus.yml and scrape targets without wiping TSDB data."

    return {
        "reload_url": f"{_prometheus_base_url(request_host)}/-/reload",
        "restart_mode": mode,
        "restart_available": restart_available,
        "wipe_tsdb_on_reload": wipe_on_reload,
        "wipe_tsdb_available": wipe_available,
        "restart_hint": restart_hint(mode),
        "reload_hint": reload_hint,
        "restart_hint_detail": (
            "Recreates the Prometheus container/pod (clears TSDB on emptyDir/ephemeral storage)."
        ),
    }
