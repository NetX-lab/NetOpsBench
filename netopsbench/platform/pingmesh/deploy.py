"""Deploy Pingmesh agents to all client containers for a topology.

Replaces ``scripts/runtime/deploy_pingmesh.sh`` with a pure-Python
implementation that provides proper error handling, structured logging,
and direct integration with the topology metadata helpers.

CLI usage (backward-compatible with the shell script)::

    python -m netopsbench.platform.pingmesh.deploy <pinglist_file> <topology_dir>

Programmatic usage::

    from netopsbench.platform.pingmesh.deploy import deploy_pingmesh
    result = deploy_pingmesh("/path/to/topology_dir")
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from netopsbench.config import config, repo_root
from netopsbench.logging_utils import get_logger
from netopsbench.platform.pingmesh.generator import generate_pinglist_from_topology
from netopsbench.platform.topology.topology_utils import clab_container_name
from netopsbench.platform.utils.events import emit as _emit
from netopsbench.platform.utils.proc import safe_run, sudo_prefix

logger = get_logger(__name__)


# Agent files that must be copied into each client container.
_AGENT_FILES: list[str] = [
    "netopsbench/platform/pingmesh/agent.py",
    "netopsbench/platform/pingmesh/cli.py",
    "netopsbench/platform/pingmesh/_agent_support.py",
    "netopsbench/platform/pingmesh/_agent_probe.py",
    "netopsbench/platform/pingmesh/_agent_responder.py",
    "netopsbench/platform/pingmesh/_agent_influx.py",
    "netopsbench/platform/pingmesh/_agent_runtime.py",
    "scripts/runtime/run_pingmesh_agent.py",
]


@dataclass
class DeployResult:
    """Summary of a Pingmesh deployment."""

    deployed: int = 0
    failed: list[str] = field(default_factory=list)
    verified: dict[str, bool] = field(default_factory=dict)


def _docker(*args: str, check: bool = True, capture: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """Run a docker command, raising on failure when *check* is set."""
    kwargs.setdefault("timeout", 60)
    return safe_run(
        [*sudo_prefix(), "docker", *args],
        check=check,
        capture_output=capture,
        text=True,
        **kwargs,
    )


def _running_containers() -> list[str]:
    result = _docker("ps", "--format", "{{.Names}}", check=False, capture=True)
    return result.stdout.strip().splitlines() if result.returncode == 0 else []


def _load_topology_metadata(topology_dir: str) -> dict:
    metadata_path = os.path.join(topology_dir, "topology.json")
    if not os.path.isfile(metadata_path):
        raise FileNotFoundError(f"Topology metadata not found: {metadata_path}")
    with open(metadata_path, encoding="utf-8") as fh:
        return json.load(fh)


def deploy_pingmesh(
    topology_dir: str,
    pinglist_file: str | None = None,
    cycle_interval: int = 1,
    influxdb_url: str | None = None,
    influxdb_token: str | None = None,
    influxdb_org: str | None = None,
    influxdb_bucket: str | None = None,
    topology_id: str | None = None,
    verify: bool = True,
) -> DeployResult:
    """Deploy Pingmesh agents to every client container in *topology_dir*.

    Returns a :class:`DeployResult` with deployment counts and per-client
    verification status.
    """
    root = str(repo_root())

    # --- resolve parameters from env with fallbacks ---
    pinglist_file = pinglist_file or os.path.join(topology_dir, "pinglist.json")
    cycle_interval = int(os.environ.get("PINGMESH_CYCLE_INTERVAL", cycle_interval))
    topology_id = topology_id or config.topology_id or os.path.basename(topology_dir)
    influxdb_url = influxdb_url or config.pingmesh_influxdb_url
    influxdb_token = influxdb_token or config.influxdb_token
    influxdb_org = influxdb_org or config.influxdb_org
    influxdb_bucket = influxdb_bucket or config.influxdb_bucket

    # --- load topology metadata ---
    topo = _load_topology_metadata(topology_dir)
    lab_name = (topo.get("name") or "dcn").strip()
    clients: list[str] = []
    for client in (topo.get("devices", {}) or {}).get("clients", []):
        name = str(client.get("name") or "").strip()
        if name:
            clients.append(name)

    if not clients:
        raise RuntimeError(f"No clients found in topology metadata: {topology_dir}/topology.json")

    # --- validate running containers ---
    _emit("=== Deploying Pingmesh Agents ===")
    _emit(f"Topology: {topology_dir}")
    _emit(f"Topology ID: {topology_id}")
    _emit(f"InfluxDB: {influxdb_url} bucket={influxdb_bucket}")
    _emit(f"Cycle interval: {cycle_interval}s")
    _emit("")

    running = set(_running_containers())
    expected = {clab_container_name(lab_name, c) for c in clients}
    running_clients = expected & running
    if not running_clients:
        raise RuntimeError(f"No client containers running for lab '{lab_name}'. " f"Deploy topology first.")
    if len(running_clients) != len(expected):
        missing = expected - running
        logger.warning(
            "%d client container(s) not running: %s",
            len(missing),
            ", ".join(sorted(missing)),
        )

    _emit(f"[0/3] Validated {len(running_clients)}/{len(expected)} client containers")
    _emit("")

    # --- generate pinglist ---
    _emit("[1/3] Generating pinglist...")
    metadata_file = os.path.join(topology_dir, "topology.json")
    generate_pinglist_from_topology(metadata_file, pinglist_file, topology_id=topology_id)
    if not os.path.isfile(pinglist_file):
        raise RuntimeError("Pinglist generation failed")
    _emit("")

    # --- deploy to each client ---
    _emit("[2/3] Deploying agents to client containers...")
    result = DeployResult()

    agent_sources = [os.path.join(root, src) for src in _AGENT_FILES]

    for client_name in clients:
        container = clab_container_name(lab_name, client_name)
        if container not in running:
            logger.warning("%s: not running, skipping", container)
            result.failed.append(client_name)
            continue

        _emit(f"  Deploying to {container}...")

        # Create directories
        ret = _docker("exec", container, "mkdir", "-p", "/tmp/pingmesh", "/var/log/pingmesh", check=False)
        if ret.returncode != 0:
            logger.error("%s: failed to create directories, skipping", container)
            result.failed.append(client_name)
            continue

        # Copy agent files
        copy_ok = True
        for src in agent_sources:
            dst = f"{container}:/tmp/pingmesh/{os.path.basename(src)}"
            ret = _docker("cp", src, dst, check=False)
            if ret.returncode != 0:
                logger.error(
                    "%s: failed to copy %s, skipping",
                    container,
                    os.path.basename(src),
                )
                copy_ok = False
                break
        if not copy_ok:
            result.failed.append(client_name)
            continue

        # Copy pinglist
        _docker("cp", pinglist_file, f"{container}:/tmp/pingmesh/pinglist.json", check=False)

        # Kill any existing agent, then start the new one
        _docker("exec", container, "pkill", "-f", "/tmp/pingmesh/run_pingmesh_agent.py", check=False, capture=True)

        env_block = (
            f"PYTHONPATH=/tmp "
            f"NETOPSBENCH_TOPOLOGY_ID='{topology_id}' "
            f"NETOPSBENCH_INFLUXDB_URL='{influxdb_url}' "
            f"NETOPSBENCH_INFLUXDB_TOKEN='{influxdb_token}' "
            f"NETOPSBENCH_INFLUXDB_ORG='{influxdb_org}' "
            f"NETOPSBENCH_INFLUXDB_BUCKET='{influxdb_bucket}' "
            f"nohup python3 /tmp/pingmesh/run_pingmesh_agent.py "
            f"/tmp/pingmesh/pinglist.json {cycle_interval} "
            f"> /var/log/pingmesh/agent.log 2>&1 &"
        )
        _docker("exec", "-d", container, "sh", "-c", env_block, check=False)
        result.deployed += 1

    if result.failed:
        logger.warning(
            "%d client(s) failed: %s",
            len(result.failed),
            ", ".join(result.failed),
        )

    # --- verify ---
    if verify:
        _emit("")
        _emit("[3/3] Verifying deployment...")
        time.sleep(2)
        for client_name in clients[:3]:
            container = clab_container_name(lab_name, client_name)
            ret = _docker(
                "exec",
                container,
                "ps",
                "aux",
                check=False,
                capture=True,
            )
            agent_running = "run_pingmesh_agent.py" in (ret.stdout or "")
            result.verified[client_name] = agent_running
            status = "✓ running" if agent_running else "✗ NOT running"
            _emit(f"  {container}: Agent {status}")

    _emit("")
    _emit("=== Pingmesh Deployment Complete ===")
    _emit(f"  Deployed to {result.deployed} clients")
    _emit(f"  Pinglist: {pinglist_file}")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point (backward-compatible with shell script args)."""
    args = list(argv if argv is not None else sys.argv[1:])

    pinglist_file = args[0] if len(args) > 0 else None
    topology_dir = args[1] if len(args) > 1 else (config.topology_dir or "generated_topology")

    try:
        deploy_pingmesh(topology_dir=topology_dir, pinglist_file=pinglist_file)
    except (FileNotFoundError, RuntimeError) as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
