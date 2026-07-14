"""Restart OpenTelemetry Collector after config changes (docker compose or Kubernetes)."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from backend.bigip_client import BigIPError

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
COLLECTOR_SERVICE = os.environ.get("COLLECTOR_COMPOSE_SERVICE", "otel-collector")
COLLECTOR_K8S_DEPLOYMENT = os.environ.get("COLLECTOR_K8S_DEPLOYMENT", "deployment/otel-collector")
COLLECTOR_K8S_CONFIGMAP = os.environ.get("COLLECTOR_K8S_CONFIGMAP", "otel-collector-config")
COLLECTOR_K8S_CONFIGMAP_KEY = os.environ.get("COLLECTOR_K8S_CONFIGMAP_KEY", "config.yaml")
SA_NAMESPACE_FILE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


def auto_restart_enabled() -> bool:
    return os.environ.get("COLLECTOR_AUTO_RESTART", "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _in_cluster() -> bool:
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST"))


def _k8s_namespace() -> str:
    if "COLLECTOR_K8S_NAMESPACE" in os.environ:
        value = os.environ["COLLECTOR_K8S_NAMESPACE"].strip()
        if value:
            return value
    if SA_NAMESPACE_FILE.is_file():
        value = SA_NAMESPACE_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    return "bigip-telemetry"


def _k8s_deployment_name() -> str:
    raw = COLLECTOR_K8S_DEPLOYMENT.strip()
    if "/" in raw:
        return raw.split("/", 1)[-1]
    return raw or "otel-collector"


def _detect_restart_mode() -> str:
    explicit = os.environ.get("COLLECTOR_RESTART_MODE", "").strip().lower()
    if explicit in ("docker", "kubernetes", "none"):
        return explicit
    if _in_cluster():
        return "kubernetes"
    if shutil.which("docker") and COMPOSE_FILE.is_file():
        return "docker"
    if shutil.which("kubectl"):
        return "kubernetes"
    return "none"


def restart_hint(mode: str) -> str:
    if mode == "docker":
        return f"docker compose -f {COMPOSE_FILE} restart {COLLECTOR_SERVICE}"
    if mode == "kubernetes":
        ns = _k8s_namespace()
        return (
            f"kubectl -n {ns} create configmap {COLLECTOR_K8S_CONFIGMAP} "
            f"--from-file={COLLECTOR_K8S_CONFIGMAP_KEY}=<path> --dry-run=client -o yaml | kubectl apply -f - "
            f"&& kubectl -n {ns} rollout restart {_k8s_deployment_name()}"
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
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), "restart", COLLECTOR_SERVICE]
    result = _run_command(cmd, cwd=str(REPO_ROOT), label="docker compose restart")
    _wait_collector_healthy()
    return {
        "ok": True,
        "mode": "docker",
        "command": result["command"],
        "message": "OpenTelemetry Collector restarted (docker compose).",
    }


def _load_k8s_clients() -> tuple[Any, Any]:
    """Return (CoreV1Api, AppsV1Api), preferring in-cluster auth on Kubernetes/OpenShift."""
    try:
        from kubernetes import client, config
    except ImportError as exc:
        raise BigIPError(
            "Python package 'kubernetes' is required for in-cluster collector config updates. "
            "Rebuild the backend image with updated requirements, or use kubectl manually: "
            f"{restart_hint('kubernetes')}"
        ) from exc

    try:
        if _in_cluster():
            config.load_incluster_config()
        else:
            config.load_kube_config()
    except Exception as exc:  # noqa: BLE001
        raise BigIPError(
            f"Could not load Kubernetes client configuration: {exc}. "
            f"Manual workaround: {restart_hint('kubernetes')}"
        ) from exc

    return client.CoreV1Api(), client.AppsV1Api()


def _sync_k8s_configmap_api(config_path: Path) -> dict[str, Any]:
    """Update the collector ConfigMap via the Kubernetes API (works in-cluster / OpenShift)."""
    if not config_path.is_file():
        raise BigIPError(f"Collector config file not found: {config_path}")

    yaml_text = config_path.read_text(encoding="utf-8")
    namespace = _k8s_namespace()
    name = COLLECTOR_K8S_CONFIGMAP
    key = COLLECTOR_K8S_CONFIGMAP_KEY
    core, _ = _load_k8s_clients()

    try:
        from kubernetes.client.rest import ApiException
    except ImportError:  # pragma: no cover
        ApiException = Exception  # type: ignore[misc, assignment]

    try:
        existing = core.read_namespaced_config_map(name=name, namespace=namespace)
        data = dict(existing.data or {})
        data[key] = yaml_text
        existing.data = data
        core.patch_namespaced_config_map(name=name, namespace=namespace, body=existing)
        action = "patched"
    except ApiException as exc:
        if getattr(exc, "status", None) != 404:
            raise BigIPError(
                f"Failed to update ConfigMap {namespace}/{name}: {exc}"
            ) from exc
        from kubernetes import client

        body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=name, namespace=namespace),
            data={key: yaml_text},
        )
        core.create_namespaced_config_map(namespace=namespace, body=body)
        action = "created"

    logger.info("Collector ConfigMap %s/%s %s via Kubernetes API", namespace, name, action)
    return {
        "method": "kubernetes_api",
        "namespace": namespace,
        "configmap": name,
        "key": key,
        "action": action,
    }


def _restart_k8s_deployment_api() -> dict[str, Any]:
    """Rollout-restart the collector Deployment by patching a pod-template annotation."""
    namespace = _k8s_namespace()
    name = _k8s_deployment_name()
    _, apps = _load_k8s_clients()
    restarted_at = datetime.now(timezone.utc).isoformat()
    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": restarted_at,
                    }
                }
            }
        }
    }
    try:
        apps.patch_namespaced_deployment(name=name, namespace=namespace, body=patch)
    except Exception as exc:  # noqa: BLE001
        raise BigIPError(
            f"Failed to restart Deployment {namespace}/{name}: {exc}"
        ) from exc

    deadline = time.time() + 120
    last_error = ""
    while time.time() < deadline:
        try:
            dep = apps.read_namespaced_deployment(name=name, namespace=namespace)
            status = dep.status
            spec = dep.spec
            desired = int(spec.replicas or 0) if spec else 0
            ready = int(status.ready_replicas or 0) if status else 0
            updated = int(status.updated_replicas or 0) if status else 0
            available = int(status.available_replicas or 0) if status else 0
            if desired == 0 or (ready >= desired and updated >= desired and available >= desired):
                return {
                    "method": "kubernetes_api",
                    "namespace": namespace,
                    "deployment": name,
                    "restarted_at": restarted_at,
                }
            last_error = f"ready={ready}/{desired} updated={updated} available={available}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(2)

    raise BigIPError(
        f"Timed out waiting for Deployment {namespace}/{name} rollout ({last_error})"
    )


def _sync_k8s_configmap_kubectl(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        raise BigIPError(f"Collector config file not found: {config_path}")
    if not shutil.which("kubectl"):
        raise BigIPError("kubectl not found in PATH")

    namespace = _k8s_namespace()
    render_cmd = [
        "kubectl",
        "-n",
        namespace,
        "create",
        "configmap",
        COLLECTOR_K8S_CONFIGMAP,
        f"--from-file={COLLECTOR_K8S_CONFIGMAP_KEY}={config_path}",
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
        "method": "kubectl",
        "configmap_command": " ".join(render_cmd),
        "apply_command": apply["command"],
    }


def _restart_kubernetes(*, config_path: Path | None) -> dict[str, Any]:
    """
    Update the collector ConfigMap and rollout-restart the Deployment.

    Prefers the in-cluster Kubernetes API (Kubernetes and OpenShift). Falls back
    to kubectl when available (laptop / CI with kubeconfig).
    """
    if _in_cluster() or not shutil.which("kubectl"):
        sync_info: dict[str, Any] = {}
        if config_path is not None:
            sync_info = _sync_k8s_configmap_api(config_path)
        restart_info = _restart_k8s_deployment_api()
        _wait_collector_healthy()
        return {
            "ok": True,
            "mode": "kubernetes",
            "method": "kubernetes_api",
            "sync": sync_info,
            "restart": restart_info,
            "message": (
                "OpenTelemetry Collector ConfigMap updated and deployment restarted "
                "(Kubernetes/OpenShift API)."
            ),
        }

    sync_info = {}
    if config_path is not None:
        sync_info = _sync_k8s_configmap_kubectl(config_path)

    namespace = _k8s_namespace()
    name = _k8s_deployment_name()
    restart = _run_command(
        ["kubectl", "-n", namespace, "rollout", "restart", f"deployment/{name}"],
        label="kubectl rollout restart",
    )
    _run_command(
        [
            "kubectl",
            "-n",
            namespace,
            "rollout",
            "status",
            f"deployment/{name}",
            "--timeout=120s",
        ],
        label="kubectl rollout status",
    )
    _wait_collector_healthy()
    return {
        "ok": True,
        "mode": "kubernetes",
        "method": "kubectl",
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


def _kubernetes_api_available() -> bool:
    try:
        import kubernetes  # noqa: F401
    except ImportError:
        return False
    return _in_cluster() or Path.home().joinpath(".kube", "config").is_file()


def control_status() -> dict[str, Any]:
    mode = _detect_restart_mode()
    restart_available = bool(os.environ.get("COLLECTOR_RESTART_CMD"))
    if mode == "docker" and shutil.which("docker") and COMPOSE_FILE.is_file():
        restart_available = True
    if mode == "kubernetes" and (
        shutil.which("kubectl") or (_in_cluster() and _kubernetes_api_available())
    ):
        restart_available = True
    return {
        "restart_mode": mode,
        "auto_restart": auto_restart_enabled(),
        "restart_available": restart_available,
        "restart_hint": restart_hint(mode),
        "health_url": _health_url(),
        "in_cluster": _in_cluster(),
        "k8s_namespace": _k8s_namespace() if mode == "kubernetes" else None,
    }
