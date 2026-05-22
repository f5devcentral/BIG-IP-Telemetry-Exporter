"""Reload or restart Prometheus for validation (docker compose, kubectl, or HTTP reload)."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import requests

from backend.bigip_client import BigIPError

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"


def _prometheus_reload_url(request_host: str | None = None) -> str:
    if url := os.environ.get("PROMETHEUS_RELOAD_URL", "").strip():
        return url.rstrip("/")
    if url := os.environ.get("PROMETHEUS_UI_URL", "").strip():
        return url.rstrip("/")
    host = (request_host or "127.0.0.1").split(":")[0]
    port = os.environ.get("PROMETHEUS_BROWSER_PORT", "9090")
    return f"http://{host}:{port}"


def reload_prometheus(*, request_host: str | None = None) -> dict[str, Any]:
    """POST /-/reload to pick up config changes and refresh scrape targets."""
    base = _prometheus_reload_url(request_host)
    url = f"{base}/-/reload"
    try:
        r = requests.post(url, timeout=30)
    except requests.RequestException as exc:
        raise BigIPError(
            f"Cannot reach Prometheus at {base} for reload ({exc}). "
            "Ensure Prometheus is running with --web.enable-lifecycle.",
        ) from exc
    if r.status_code == 200:
        return {"ok": True, "action": "reload", "url": url, "message": "Prometheus configuration reloaded."}
    if r.status_code == 404:
        raise BigIPError(
            "Prometheus lifecycle API disabled. Add --web.enable-lifecycle to Prometheus "
            "or use Restart (docker compose / kubectl) instead.",
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
    ns = os.environ.get("PROMETHEUS_K8S_NAMESPACE", "bigip-metrics")
    if mode == "docker":
        return f"docker compose -f {COMPOSE_FILE} restart prometheus"
    if mode == "kubernetes":
        return f"kubectl -n {ns} rollout restart deployment/prometheus"
    return "Reload via API or restart Prometheus manually."


def restart_prometheus() -> dict[str, Any]:
    """Restart the Prometheus process/container (stronger refresh than reload)."""
    if cmd := os.environ.get("PROMETHEUS_RESTART_CMD", "").strip():
        return _run_restart_command(shlex.split(cmd), label="PROMETHEUS_RESTART_CMD")

    mode = _detect_restart_mode()
    if mode == "none":
        raise BigIPError(
            "Automatic Prometheus restart is not available in this environment. "
            f"Try Reload instead, or run manually: {restart_hint('docker')}",
        )

    if mode == "docker":
        if not COMPOSE_FILE.is_file():
            raise BigIPError(f"docker-compose.yml not found at {COMPOSE_FILE}")
        if not shutil.which("docker"):
            raise BigIPError("docker not found in PATH")
        cmd = [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "restart",
            "prometheus",
        ]
        return _run_restart_command(cmd, cwd=str(REPO_ROOT), label="docker compose")

    ns = os.environ.get("PROMETHEUS_K8S_NAMESPACE", "bigip-metrics")
    if not shutil.which("kubectl"):
        raise BigIPError(
            "kubectl not found. From a machine with cluster access run: "
            f"{restart_hint('kubernetes')}",
        )
    cmd = [
        "kubectl",
        "rollout",
        "restart",
        "deployment/prometheus",
        "-n",
        ns,
        "--timeout=120s",
    ]
    return _run_restart_command(cmd, label="kubectl")


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
        raise BigIPError(f"Prometheus restart timed out ({label})") from exc
    except OSError as exc:
        raise BigIPError(f"Prometheus restart failed to run ({label}): {exc}") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        raise BigIPError(f"Prometheus restart failed ({label}, exit {proc.returncode}): {detail}")

    return {
        "ok": True,
        "action": "restart",
        "command": " ".join(cmd),
        "message": "Prometheus restarted. Wait a few seconds, then check Targets in the UI.",
        "stdout": (proc.stdout or "").strip()[:300],
    }


def control_status(*, request_host: str | None = None) -> dict[str, Any]:
    mode = _detect_restart_mode()
    restart_available = bool(os.environ.get("PROMETHEUS_RESTART_CMD"))
    if mode == "docker" and shutil.which("docker") and COMPOSE_FILE.is_file():
        restart_available = True
    if mode == "kubernetes" and shutil.which("kubectl"):
        restart_available = True
    return {
        "reload_url": f"{_prometheus_reload_url(request_host)}/-/reload",
        "restart_mode": mode,
        "restart_available": restart_available,
        "restart_hint": restart_hint(mode),
        "reload_hint": "Reloads prometheus.yml and scrape targets without wiping TSDB data.",
        "restart_hint_detail": "Stops and starts the Prometheus container/pod (fresh process).",
    }
