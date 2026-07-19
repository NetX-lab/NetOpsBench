"""Idempotent observability lifecycle operations for runtime workers."""

from __future__ import annotations

import os
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

from netopsbench.config import config
from netopsbench.logging_utils import get_logger
from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.platform.observability.influxdb import ensure_bucket, ensure_pingmesh_aggregate_tasks
from netopsbench.platform.observability.telegraf import update_telegraf_config
from netopsbench.platform.utils.proc import docker_prefix, safe_run

BGP_POLL_INTERVAL_SECONDS = 10
BGP_COLLECTOR_PARALLELISM = 16
INTERNAL_INFLUXDB_URL = "http://influxdb:8086"
logger = get_logger(__name__)


def observability_asset_root() -> Path:
    root = files("netopsbench.platform.observability").joinpath("assets")
    if not root.is_dir():
        raise FileNotFoundError("Packaged observability assets are missing")
    return Path(str(root))


def ensure_observability_core() -> None:
    root = observability_asset_root()
    safe_run(
        [
            *docker_prefix(),
            "docker",
            "compose",
            "--project-name",
            "observability",
            "--project-directory",
            str(root),
            "-f",
            str(root / "docker-compose.yaml"),
            "up",
            "-d",
            "influxdb",
            "grafana",
        ],
        cwd=root,
        check=True,
        timeout=600,
    )


def ensure_worker_observability(worker: RuntimeIdentity) -> None:
    """Reconcile the shared core, worker collector, and Telegraf sidecar."""
    ensure_observability_core()
    ensure_bucket(
        config.influxdb_url,
        config.influxdb_token,
        config.influxdb_org,
        worker.bucket,
    )
    try:
        ensure_pingmesh_aggregate_tasks(
            config.influxdb_url,
            config.influxdb_token,
            config.influxdb_org,
            bucket=worker.bucket,
            topology_id=worker.topology_id,
            runtime_id=worker.runtime_id,
            worker_index=worker.worker_index,
        )
    except Exception:
        logger.warning(
            "InfluxDB Tasks API unavailable; Pingmesh queries will use raw-data fallback for %s",
            worker.lab_name,
            exc_info=True,
        )
    docker = [*docker_prefix(), "docker"]
    safe_run([*docker, "inspect", "influxdb"], check=True, timeout=30)
    safe_run(
        [*docker, "network", "connect", "--alias", "influxdb", worker.mgmt_network, "influxdb"],
        check=False,
        timeout=60,
    )
    ensure_worker_bgp_collector(worker)
    ensure_worker_telegraf(worker)


def ensure_worker_telegraf(worker: RuntimeIdentity) -> None:
    topology_dir = worker.topology_dir.resolve()
    topology_file = topology_dir / "topology.json"
    if not topology_file.is_file():
        raise FileNotFoundError(f"Topology metadata not found: {topology_file}")

    container_name = f"telegraf-{worker.lab_name}"
    config_path = topology_dir / f"{container_name}.conf"
    bgp_file = topology_dir / "bgp_neighbors.lp"
    update_telegraf_config(
        str(topology_file),
        output_file=str(config_path),
        influxdb_url=INTERNAL_INFLUXDB_URL,
        influxdb_token=config.influxdb_token,
        influxdb_org=config.influxdb_org,
        influxdb_bucket=worker.bucket,
        topology_id=worker.topology_id,
    )
    bgp_file.touch(exist_ok=True)
    topology_dir.chmod(0o755)
    config_path.chmod(0o644)
    bgp_file.chmod(0o644)

    docker = [*docker_prefix(), "docker"]
    safe_run([*docker, "rm", "-f", container_name], check=False, timeout=60)
    safe_run(
        [
            *docker,
            "run",
            "-d",
            "--name",
            container_name,
            "--restart",
            "unless-stopped",
            "--network",
            worker.mgmt_network,
            "--ip",
            _collector_ip(topology_file),
            "-v",
            f"{config_path}:/etc/telegraf/telegraf.conf:ro",
            "-v",
            f"{topology_dir}:/var/lib/netopsbench:ro",
            "telegraf:latest",
        ],
        check=True,
        timeout=600,
    )


def ensure_worker_bgp_collector(worker: RuntimeIdentity) -> None:
    topology_dir = worker.topology_dir
    topology_file = topology_dir / "topology.json"
    if not topology_file.is_file():
        raise FileNotFoundError(f"Topology metadata not found: {topology_file}")

    pid_file = topology_dir / "bgp_collector.pid"
    output_file = topology_dir / "bgp_neighbors.lp"
    log_file = topology_dir / "bgp_collector.log"
    output_file.touch(exist_ok=True)
    if _bgp_collector_is_running(pid_file, topology_file):
        return
    pid_file.unlink(missing_ok=True)

    command = [
        sys.executable,
        "-m",
        "netopsbench.platform.observability.bgp_collector",
        str(topology_file),
        "--output",
        str(output_file),
        "--interval",
        str(BGP_POLL_INTERVAL_SECONDS),
        "--parallelism",
        str(BGP_COLLECTOR_PARALLELISM),
        "--topology-id",
        worker.topology_id,
    ]
    with log_file.open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=topology_dir,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_file.write_text(f"{process.pid}\n", encoding="utf-8")


def _bgp_collector_is_running(pid_file: Path, topology_file: Path) -> bool:
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        command_line = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except (FileNotFoundError, OSError, ValueError):
        return False
    return "netopsbench.platform.observability.bgp_collector" in command_line and str(topology_file) in command_line


def _collector_ip(topology_file: Path) -> str:
    from netopsbench.platform.topology.topology_utils import load_topology_manifest

    return load_topology_manifest(topology_file).collector.ipv4


__all__ = [
    "ensure_observability_core",
    "ensure_worker_bgp_collector",
    "ensure_worker_observability",
    "ensure_worker_telegraf",
    "observability_asset_root",
]
