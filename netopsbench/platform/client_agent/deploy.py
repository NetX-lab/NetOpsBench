"""Start the two native client-agent processes in every client container."""

from __future__ import annotations

import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from netopsbench.config import config
from netopsbench.logging_utils import get_logger
from netopsbench.platform.topology.topology_utils import clab_container_name, load_topology_manifest
from netopsbench.platform.utils.proc import docker_prefix, safe_run

from .config import CLIENT_AGENT_CONFIG_NAME, write_client_agent_config
from .contract import (
    HEARTBEAT_MAX_AGE_SECONDS,
    PINGMESH_CONTROL_PORT,
    TRAFFIC_CONTROL_PORT,
)
from .control import request_agent

logger = get_logger(__name__)

CLIENT_AGENT_BINARY = "/usr/local/bin/netopsbench-client-agent"
CLIENT_AGENT_CONFIG = f"/etc/netopsbench/{CLIENT_AGENT_CONFIG_NAME}"
CLIENT_AGENT_BIND = "configs/client-agent:/etc/netopsbench:ro"
PINGMESH_INGEST_URL = "http://telegraf:8186"
DEFAULT_DEPLOY_PARALLELISM = 32
READINESS_TIMEOUT_SECONDS = 15.0


@dataclass
class DeployResult:
    deployed: int = 0
    failed: list[str] = field(default_factory=list)


def _docker(*args: str, check: bool = True, capture: bool = False, **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("timeout", 60)
    return safe_run(
        [*docker_prefix(), "docker", *args],
        check=check,
        capture_output=capture,
        text=True,
        **kwargs,
    )


def _running_containers() -> set[str]:
    result = _docker("ps", "--format", "{{.Names}}", check=False, capture=True)
    return set(result.stdout.strip().splitlines()) if result.returncode == 0 else set()


def _topology_yaml_path(topology_dir: Path, lab_name: str) -> Path:
    candidate = topology_dir / f"{lab_name}.clab.yaml"
    if candidate.is_file():
        return candidate
    matches = sorted(topology_dir.glob("*.clab.yaml"))
    return matches[0] if matches else candidate


def _validate_bind(topology_dir: Path, lab_name: str) -> None:
    yaml_path = _topology_yaml_path(topology_dir, lab_name)
    if not yaml_path.is_file():
        raise RuntimeError(f"Containerlab topology YAML not found: {yaml_path}")
    topology = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    linux_kind = ((topology.get("topology") or {}).get("kinds") or {}).get("linux") or {}
    if CLIENT_AGENT_BIND not in (linux_kind.get("binds") or []):
        raise RuntimeError(
            f"Native client-agent bind missing from {yaml_path}: expected {CLIENT_AGENT_BIND}. "
            "Regenerate the topology."
        )


def _env_assignment(name: str, value: str) -> str:
    return f"{name}={shlex.quote(value)}"


def _stop_processes_command(*, exit_with_status: bool = True) -> str:
    command = (
        "status=0; "
        "for mode in pingmesh traffic; do "
        '  pid_file="/run/netopsbench/$mode.pid"; '
        '  if [ -r "$pid_file" ]; then '
        '    pid="$(cat "$pid_file" 2>/dev/null || true)"; '
        f'    if [ -n "$pid" ] && [ "$(readlink "/proc/$pid/exe" 2>/dev/null || true)" = "{CLIENT_AGENT_BINARY}" ]; then '
        '      kill "$pid" >/dev/null 2>&1 || status=1; '
        "      attempt=0; "
        '      while kill -0 "$pid" >/dev/null 2>&1; do '
        "        attempt=$((attempt + 1)); "
        '        if [ "$attempt" -ge 50 ]; then '
        f'          if [ "$(readlink "/proc/$pid/exe" 2>/dev/null || true)" = "{CLIENT_AGENT_BINARY}" ]; then '
        '            kill -KILL "$pid" >/dev/null 2>&1 || status=1; '
        "          fi; "
        "          break; "
        "        fi; "
        "        sleep 0.1; "
        "      done; "
        "    fi; "
        '    rm -f "$pid_file"; '
        "  fi; "
        "done; "
    )
    return command + ('exit "$status"' if exit_with_status else "")


def _stop_client_agent(container: str) -> str | None:
    result = _docker(
        "exec",
        container,
        "sh",
        "-c",
        _stop_processes_command(),
        check=False,
        capture=True,
        timeout=15,
    )
    if result.returncode == 0:
        return None
    return (result.stderr or result.stdout or "").strip() or f"exit status {result.returncode}"


def _cleanup_started_clients(containers: list[str], parallelism: int) -> list[str]:
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=min(max(1, int(parallelism)), max(1, len(containers)))) as executor:
        futures = {executor.submit(_stop_client_agent, container): container for container in containers}
        for future in as_completed(futures):
            container = futures[future]
            try:
                message = future.result()
            except Exception as exc:  # noqa: BLE001 - preserve every cleanup failure
                message = f"{type(exc).__name__}: {exc}"
            if message:
                failures.append(f"{container}: {message}")
    return sorted(failures)


def _wait_until_ready(management_ip: str) -> None:
    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    last_error = "status endpoint unavailable"
    while time.monotonic() < deadline:
        try:
            pingmesh = request_agent(management_ip, PINGMESH_CONTROL_PORT, "status")
            traffic = request_agent(management_ip, TRAFFIC_CONTROL_PORT, "status")
            pingmesh_status = pingmesh.get("status") or {}
            heartbeat_ns = int(pingmesh_status.get("heartbeat_unix_ns", 0) or 0)
            heartbeat_age = time.time() - heartbeat_ns / 1_000_000_000
            if (
                pingmesh_status.get("ready") is True
                and 0 <= heartbeat_age <= HEARTBEAT_MAX_AGE_SECONDS
                and traffic.get("ok") is True
            ):
                return
            last_error = "Pingmesh process is not ready"
        except (OSError, RuntimeError, ValueError) as exc:
            last_error = str(exc)
        time.sleep(0.1)
    raise RuntimeError(f"native client-agent readiness failed: {last_error}")


def _start_client(
    *,
    client_name: str,
    container: str,
    management_ip: str,
    influxdb_token: str,
    influxdb_org: str,
    influxdb_bucket: str,
) -> tuple[str, bool, str]:
    env_values = {
        "NETOPSBENCH_INFLUXDB_URL": PINGMESH_INGEST_URL,
        "NETOPSBENCH_INFLUXDB_TOKEN": influxdb_token,
        "NETOPSBENCH_INFLUXDB_ORG": influxdb_org,
        "NETOPSBENCH_INFLUXDB_BUCKET": influxdb_bucket,
    }
    environment = " ".join(_env_assignment(name, value) for name, value in env_values.items())
    command = (
        "set -e; "
        f"test -x {CLIENT_AGENT_BINARY}; "
        f"test -r {CLIENT_AGENT_CONFIG}; "
        "mkdir -p /run/netopsbench /var/log/netopsbench; "
        f"{_stop_processes_command(exit_with_status=False)} "
        '[ "$status" -eq 0 ]; '
        f"{environment} nohup {CLIENT_AGENT_BINARY} pingmesh --config {CLIENT_AGENT_CONFIG} "
        "> /var/log/netopsbench/pingmesh.log 2>&1 </dev/null & "
        'printf "%s\\n" "$!" > /run/netopsbench/pingmesh.pid; '
        f"nohup {CLIENT_AGENT_BINARY} traffic --config {CLIENT_AGENT_CONFIG} "
        "> /var/log/netopsbench/traffic.log 2>&1 </dev/null & "
        'printf "%s\\n" "$!" > /run/netopsbench/traffic.pid; '
        "sleep 0.2; "
        'kill -0 "$(cat /run/netopsbench/pingmesh.pid)"; '
        'kill -0 "$(cat /run/netopsbench/traffic.pid)"'
    )
    result = _docker("exec", container, "sh", "-c", command, check=False, capture=True, timeout=30)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or f"exit status {result.returncode}"
        return client_name, False, detail
    try:
        _wait_until_ready(management_ip)
    except RuntimeError as exc:
        return client_name, False, str(exc)
    return client_name, True, ""


def deploy_client_agents(
    topology_dir: str,
    *,
    influxdb_token: str | None = None,
    influxdb_org: str | None = None,
    influxdb_bucket: str | None = None,
    parallelism: int = DEFAULT_DEPLOY_PARALLELISM,
) -> DeployResult:
    root = Path(topology_dir)
    manifest = load_topology_manifest(root / "topology.json")
    clients = manifest.clients()
    if not clients:
        raise RuntimeError("Native client-agent deployment requires at least one client")
    if any(not client.mgmt_ip for client in clients):
        raise RuntimeError("Every native client-agent requires a management IP")
    _validate_bind(root, manifest.name)
    config_path = root / "configs" / "client-agent" / CLIENT_AGENT_CONFIG_NAME
    write_client_agent_config(manifest, config_path)

    running = _running_containers()
    result = DeployResult()
    outcomes: dict[str, tuple[bool, str]] = {}
    scheduled_containers: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, int(parallelism))) as executor:
        futures = {}
        for client in clients:
            client_name = client.name
            container = clab_container_name(manifest.name, client_name)
            if container not in running:
                outcomes[client_name] = (False, "container is not running")
                continue
            future = executor.submit(
                _start_client,
                client_name=client_name,
                container=container,
                management_ip=str(client.mgmt_ip),
                influxdb_token=influxdb_token or config.influxdb_token,
                influxdb_org=influxdb_org or config.influxdb_org,
                influxdb_bucket=influxdb_bucket or config.influxdb_bucket,
            )
            futures[future] = client_name
            scheduled_containers.append(container)
        for future in as_completed(futures):
            client_name, ok, message = future.result()
            outcomes[client_name] = (ok, message)

    for client in clients:
        client_name = client.name
        ok, message = outcomes.get(client_name, (False, "not scheduled"))
        if ok:
            result.deployed += 1
        else:
            result.failed.append(client_name)
            logger.error("Native client-agent failed on %s: %s", client_name, message)

    if result.deployed != len(clients):
        cleanup_failures = _cleanup_started_clients(scheduled_containers, parallelism)
        cleanup_detail = f"; cleanup_failed={'; '.join(cleanup_failures)}" if cleanup_failures else ""
        raise RuntimeError(
            f"Native client-agent deployment incomplete: {result.deployed}/{len(clients)}; "
            f"failed={','.join(result.failed)}{cleanup_detail}"
        )
    return result


__all__ = [
    "CLIENT_AGENT_BINARY",
    "CLIENT_AGENT_BIND",
    "CLIENT_AGENT_CONFIG",
    "DeployResult",
    "deploy_client_agents",
]
