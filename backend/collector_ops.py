"""Restart OpenTelemetry Collector after config changes (docker compose or kubectl)."""

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
COLLECTOR_SERVICE = os.environ.get("COLLECTOR_COMPOSE_SERVICE", "otel-collector")
PROMETHEUS_COMPOSE_SERVICE = os.environ.get("PROMETHEUS_COMPOSE_SERVICE", "prometheus")
COLLECTOR_K8S_DEPLOYMENT = os.environ.get("COLLECTOR_K8S_DEPLOYMENT", "deployment/otel-collector")
PROMETHEUS_K8S_DEPLOYMENT = os.environ.get("PROMETHEUS_K8S_DEPLOYMENT", "deployment/prometheus")
COLLECTOR_K8S_NAMESPACE = os.environ.get("COLLECTOR_K8S_NAMESPACE", "bigip-telemetry")
COLLECTOR_K8S_CONFIGMAP = os.environ.get("COLLECTOR_K8S_CONFIGMAP", "otel-collector-config")


def auto_restart_enabled() -> bool:
    return os.environ.get("COLLECTOR_AUTO_RESTART", "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _detect_restart_mode() -> str:
    explicit = os.environ.get("COLLECTOR_RESTART_MODE", "").strip().lower()
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
    if mode == "docker":
        return (
            f"docker compose -f {COMPOSE_FILE} up -d --force-recreate {COLLECTOR_SERVICE}"
        )
    if mode == "kubernetes":
        return (
            f"kubectl -n {COLLECTOR_K8S_NAMESPACE} create configmap {COLLECTOR_K8S_CONFIGMAP} "
            f"--from-file=config.yaml=<path> --dry-run=client -o yaml | kubectl apply -f - "
            f"&& kubectl -n {COLLECTOR_K8S_NAMESPACE} rollout restart {COLLECTOR_K8S_DEPLOYMENT}"
        )
    return "Restart the OpenTelemetry Collector manually to load the new config."


def _health_url() -> str:
    if url := os.environ.get("COLLECTOR_HEALTH_URL", "").strip():
        return url.rstrip("/")
    host = os.environ.get("COLLECTOR_HEALTH_HOST", "127.0.0.1").strip()
    port = os.environ.get("COLLECTOR_HEALTH_PORT", "13133")
    return f"http://{host}:{port}"


def _wait_collector_healthy(*, timeout_sec: float = 90.0) -> None:
    url = _health_url()
    deadline = time.time() + timeout_sec
    last_error = ""
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                return
            last_error = f"status {r.status_code}"
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(2)
    raise BigIPError(
        f"OpenTelemetry Collector did not become healthy at {url} within {int(timeout_sec)}s "
        f"({last_error})",
    )


def _prometheus_scrape_url() -> str:
    if url := os.environ.get("COLLECTOR_PROMETHEUS_URL", "").strip():
        return url.rstrip("/")
    host = os.environ.get("COLLECTOR_HEALTH_HOST", "127.0.0.1").strip()
    port = os.environ.get("COLLECTOR_PROMETHEUS_PORT", "8889")
    return f"http://{host}:{port}/metrics"


def probe_prometheus_scrape(*, timeout_sec: float = 30.0) -> dict[str, Any]:
    """
    Confirm the collector Prometheus exporter is reachable from the API host.

    Used after config apply so operators know metrics are scrapeable (port :8889)
    on the same machine as docker compose — not necessarily the browser machine
    when only the UI port is forwarded.
    """
    url = _prometheus_scrape_url()
    deadline = time.time() + timeout_sec
    last_error = ""
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                body = r.text or ""
                return {
                    "ok": True,
                    "url": url,
                    "bytes": len(body),
                    "has_samples": ("#" in body) or ("\n" in body and len(body) > 0),
                }
            last_error = f"status {r.status_code}"
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(2)
    return {"ok": False, "url": url, "error": last_error or "timeout"}


def _run_command(
    cmd: list[str],
    *,
    cwd: str | None = None,
    input_text: str | None = None,
    label: str,
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BigIPError(f"Collector operation timed out ({label})") from exc
    except OSError as exc:
        raise BigIPError(f"Collector operation failed to run ({label}): {exc}") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        raise BigIPError(
            f"Collector operation failed ({label}, exit {proc.returncode}): {detail}",
        )

    return {
        "ok": True,
        "command": " ".join(cmd),
        "stdout": (proc.stdout or "").strip()[:300],
    }


def _restart_docker() -> dict[str, Any]:
    if not COMPOSE_FILE.is_file():
        raise BigIPError(f"docker-compose.yml not found at {COMPOSE_FILE}")
    if not shutil.which("docker"):
        raise BigIPError("docker not found in PATH")
    # Prefer up --force-recreate so exited containers come back (restart alone does not).
    cmd = [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "up",
        "-d",
        "--force-recreate",
        COLLECTOR_SERVICE,
    ]
    result = _run_command(cmd, cwd=str(REPO_ROOT), label="docker compose up otel-collector")
    _wait_collector_healthy()
    return {
        "ok": True,
        "mode": "docker",
        "command": result["command"],
        "message": "OpenTelemetry Collector recreated (docker compose).",
    }


def _sync_k8s_configmap(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        raise BigIPError(f"Collector config file not found: {config_path}")
    if not shutil.which("kubectl"):
        raise BigIPError("kubectl not found in PATH")

    render_cmd = [
        "kubectl",
        "-n",
        COLLECTOR_K8S_NAMESPACE,
        "create",
        "configmap",
        COLLECTOR_K8S_CONFIGMAP,
        f"--from-file=config.yaml={config_path}",
        "--dry-run=client",
        "-o",
        "yaml",
    ]
    try:
        render_proc = subprocess.run(
            render_cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BigIPError(f"Collector ConfigMap render failed: {exc}") from exc
    if render_proc.returncode != 0:
        detail = (render_proc.stderr or render_proc.stdout or "").strip()[:500]
        raise BigIPError(f"Collector ConfigMap render failed: {detail}")

    apply = _run_command(
        ["kubectl", "apply", "-f", "-"],
        input_text=render_proc.stdout,
        label="kubectl apply configmap",
    )
    return {
        "configmap_command": " ".join(render_cmd),
        "apply_command": apply["command"],
    }


def _restart_kubernetes(*, config_path: Path | None) -> dict[str, Any]:
    if not shutil.which("kubectl"):
        raise BigIPError(
            "kubectl not found. From a machine with cluster access run: "
            f"{restart_hint('kubernetes')}",
        )
    sync_info: dict[str, Any] = {}
    if config_path is not None:
        sync_info = _sync_k8s_configmap(config_path)

    restart = _run_command(
        [
            "kubectl",
            "-n",
            COLLECTOR_K8S_NAMESPACE,
            "rollout",
            "restart",
            COLLECTOR_K8S_DEPLOYMENT,
        ],
        label="kubectl rollout restart",
    )
    _run_command(
        [
            "kubectl",
            "-n",
            COLLECTOR_K8S_NAMESPACE,
            "rollout",
            "status",
            COLLECTOR_K8S_DEPLOYMENT,
            "--timeout=120s",
        ],
        label="kubectl rollout status",
    )
    _wait_collector_healthy()
    return {
        "ok": True,
        "mode": "kubernetes",
        "command": restart["command"],
        "sync": sync_info,
        "message": "OpenTelemetry Collector ConfigMap updated and deployment restarted.",
    }


def restart_collector(*, config_path: Path | None = None) -> dict[str, Any]:
    """Reload the running collector after generated-config.yaml was written."""
    if cmd := os.environ.get("COLLECTOR_RESTART_CMD", "").strip():
        result = _run_command(shlex.split(cmd), label="COLLECTOR_RESTART_CMD")
        _wait_collector_healthy()
        return {
            "ok": True,
            "mode": "custom",
            "command": result["command"],
            "message": "OpenTelemetry Collector restart command completed.",
        }

    mode = _detect_restart_mode()
    if mode == "none":
        raise BigIPError(
            "Automatic collector restart is not available in this environment. "
            f"Run manually: {restart_hint('docker')}",
        )
    if mode == "docker":
        return _restart_docker()
    return _restart_kubernetes(config_path=config_path)


def _reset_prometheus_docker() -> dict[str, Any]:
    """Recreate the Prometheus container so TSDB data is wiped (fresh install)."""
    if not COMPOSE_FILE.is_file():
        raise BigIPError(f"docker-compose.yml not found at {COMPOSE_FILE}")
    if not shutil.which("docker"):
        raise BigIPError("docker not found in PATH")

    base = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    stop = _run_command(
        [*base, "stop", PROMETHEUS_COMPOSE_SERVICE],
        cwd=str(REPO_ROOT),
        label="docker compose stop prometheus",
    )
    # -v removes anonymous volumes if any; force-recreate clears container-local TSDB.
    rm = _run_command(
        [*base, "rm", "-f", "-v", PROMETHEUS_COMPOSE_SERVICE],
        cwd=str(REPO_ROOT),
        label="docker compose rm prometheus",
    )
    up = _run_command(
        [*base, "up", "-d", "--force-recreate", PROMETHEUS_COMPOSE_SERVICE],
        cwd=str(REPO_ROOT),
        label="docker compose up prometheus",
    )
    return {
        "ok": True,
        "mode": "docker",
        "command": " ; ".join([stop["command"], rm["command"], up["command"]]),
        "message": "Prometheus recreated with empty TSDB (fresh install via docker compose).",
        "fresh_install": True,
    }


def _reset_prometheus_kubernetes() -> dict[str, Any]:
    """
    Recreate Prometheus pods so emptyDir data is discarded (fresh TSDB).

    The stock manifests mount an emptyDir at /prometheus; deleting pods yields a
    clean volume. If a PVC is used instead, set PROMETHEUS_RESTART_CMD to wipe it.
    """
    if not shutil.which("kubectl"):
        raise BigIPError(
            "kubectl not found. Recreate Prometheus manually: "
            f"kubectl -n {COLLECTOR_K8S_NAMESPACE} delete pod "
            f"-l app.kubernetes.io/name=prometheus --wait=true",
        )

    delete = _run_command(
        [
            "kubectl",
            "-n",
            COLLECTOR_K8S_NAMESPACE,
            "delete",
            "pod",
            "-l",
            "app.kubernetes.io/name=prometheus",
            "--wait=true",
            "--timeout=120s",
        ],
        label="kubectl delete prometheus pods",
    )
    _run_command(
        [
            "kubectl",
            "-n",
            COLLECTOR_K8S_NAMESPACE,
            "rollout",
            "status",
            PROMETHEUS_K8S_DEPLOYMENT,
            "--timeout=120s",
        ],
        label="kubectl rollout status prometheus",
    )
    return {
        "ok": True,
        "mode": "kubernetes",
        "command": delete["command"],
        "message": (
            "Prometheus pods recreated with empty TSDB "
            "(fresh install; emptyDir wiped)."
        ),
        "fresh_install": True,
    }


def restart_prometheus() -> dict[str, Any]:
    """Wipe Prometheus time-series data and bring it back as a fresh install."""
    if cmd := os.environ.get("PROMETHEUS_RESTART_CMD", "").strip():
        result = _run_command(shlex.split(cmd), label="PROMETHEUS_RESTART_CMD")
        return {
            "ok": True,
            "mode": "custom",
            "command": result["command"],
            "message": "Prometheus reset command completed.",
            "fresh_install": True,
        }

    mode = _detect_restart_mode()
    if mode == "docker":
        return _reset_prometheus_docker()
    if mode == "kubernetes":
        return _reset_prometheus_kubernetes()
    raise BigIPError(
        "Automatic Prometheus reset is not available in this environment. "
        f"Run manually: docker compose -f {COMPOSE_FILE} up -d --force-recreate "
        f"{PROMETHEUS_COMPOSE_SERVICE}",
    )


def control_status() -> dict[str, Any]:
    mode = _detect_restart_mode()
    restart_available = bool(os.environ.get("COLLECTOR_RESTART_CMD"))
    if mode == "docker" and shutil.which("docker") and COMPOSE_FILE.is_file():
        restart_available = True
    if mode == "kubernetes" and shutil.which("kubectl"):
        restart_available = True
    return {
        "restart_mode": mode,
        "auto_restart": auto_restart_enabled(),
        "restart_available": restart_available,
        "restart_hint": restart_hint(mode),
        "health_url": _health_url(),
    }
