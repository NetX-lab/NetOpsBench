"""Deploy Pingmesh agents to all client containers for a topology.

Replaces ``scripts/runtime/deploy_pingmesh.sh`` with a pure-Python
implementation that stages the agent runtime once on the host, relies on the
Containerlab ``configs/pingmesh:/tmp/pingmesh:ro`` bind for clients, and starts
agents with bounded parallelism.

Programmatic usage::

    from netopsbench.platform.pingmesh.deploy import deploy_pingmesh
    result = deploy_pingmesh("/path/to/topology_dir")
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

import yaml

from netopsbench.config import config
from netopsbench.logging_utils import get_logger
from netopsbench.platform.pingmesh.generator import generate_pinglist_from_topology
from netopsbench.platform.topology.topology_utils import clab_container_name, load_topology_manifest
from netopsbench.platform.utils.proc import docker_prefix, safe_run

logger = get_logger(__name__)


# Agent files staged once under configs/pingmesh and bind-mounted into clients.
_AGENT_MODULES: tuple[str, ...] = (
    "agent.py",
    "cli.py",
    "_agent_probe.py",
    "_agent_responder.py",
    "_agent_influx.py",
    "_agent_runtime.py",
)
_PACKAGE_MODULES: tuple[str, ...] = ("config.py", "logging_utils.py")
_RUNTIME_SUBDIR = os.path.join("configs", "pingmesh")
_CONTAINER_RUNTIME_DIR = "/tmp/pingmesh"
_CONTAINER_PINGLIST = "/tmp/pingmesh/pinglist.json"
_CONTAINER_AGENT_MODULE = "netopsbench.platform.pingmesh.cli"
_CONTAINER_AGENT_SOURCE = "/tmp/pingmesh/netopsbench/platform/pingmesh/cli.py"
_PINGMESH_BIND = "configs/pingmesh:/tmp/pingmesh:ro"
_DEFAULT_DEPLOY_PARALLELISM = 32
_PINGMESH_INGEST_URL = "http://telegraf:8186"


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
        [*docker_prefix(), "docker", *args],
        check=check,
        capture_output=capture,
        text=True,
        **kwargs,
    )


def _running_containers() -> list[str]:
    result = _docker("ps", "--format", "{{.Names}}", check=False, capture=True)
    return result.stdout.strip().splitlines() if result.returncode == 0 else []


def _load_topology_metadata(topology_dir: str):
    metadata_path = os.path.join(topology_dir, "topology.json")
    if not os.path.isfile(metadata_path):
        raise FileNotFoundError(f"Topology metadata not found: {metadata_path}")
    return load_topology_manifest(metadata_path)


def _runtime_dir(topology_dir: str) -> str:
    return os.path.join(topology_dir, _RUNTIME_SUBDIR)


def _staged_pinglist_path(topology_dir: str) -> str:
    return os.path.join(_runtime_dir(topology_dir), "pinglist.json")


def _copy_resource(source, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())


def _stage_runtime(topology_dir: str, pinglist_file: str, topology_id: str) -> str:
    runtime_dir = _runtime_dir(topology_dir)
    os.makedirs(runtime_dir, exist_ok=True)

    staged_pinglist = _staged_pinglist_path(topology_dir)
    metadata_file = os.path.join(topology_dir, "topology.json")
    generate_pinglist_from_topology(metadata_file, staged_pinglist, topology_id=topology_id)
    if not os.path.isfile(staged_pinglist):
        raise RuntimeError("Pinglist generation failed")

    requested_pinglist = os.path.abspath(pinglist_file)
    if requested_pinglist != os.path.abspath(staged_pinglist):
        requested_dir = os.path.dirname(requested_pinglist)
        if requested_dir:
            os.makedirs(requested_dir, exist_ok=True)
        shutil.copy2(staged_pinglist, requested_pinglist)

    staged_root = Path(runtime_dir)
    package_dir = staged_root / "netopsbench" / "platform" / "pingmesh"
    for package_init in (
        staged_root / "netopsbench" / "__init__.py",
        staged_root / "netopsbench" / "platform" / "__init__.py",
        package_dir / "__init__.py",
    ):
        package_init.parent.mkdir(parents=True, exist_ok=True)
        package_init.write_text("", encoding="utf-8")

    pingmesh_resources = files("netopsbench.platform.pingmesh")
    for module in _AGENT_MODULES:
        source = pingmesh_resources.joinpath(module)
        if not source.is_file():
            raise FileNotFoundError(f"Packaged Pingmesh runtime source not found: {module}")
        _copy_resource(source, package_dir / module)
    package_resources = files("netopsbench")
    for module in _PACKAGE_MODULES:
        source = package_resources.joinpath(module)
        if not source.is_file():
            raise FileNotFoundError(f"Packaged Pingmesh dependency not found: {module}")
        _copy_resource(source, staged_root / "netopsbench" / module)

    required = [staged_pinglist, str(package_dir / "cli.py")]
    missing = [path for path in required if not os.path.isfile(path)]
    if missing:
        raise RuntimeError(f"Pingmesh runtime staging failed: missing {', '.join(missing)}")
    return staged_pinglist


def _topology_yaml_path(topology_dir: str, lab_name: str) -> str:
    candidate = os.path.join(topology_dir, f"{lab_name}.clab.yaml")
    if os.path.isfile(candidate):
        return candidate
    matches = sorted(
        os.path.join(topology_dir, name) for name in os.listdir(topology_dir) if name.endswith(".clab.yaml")
    )
    return matches[0] if matches else candidate


def _validate_pingmesh_bind(topology_dir: str, lab_name: str) -> None:
    yaml_path = _topology_yaml_path(topology_dir, lab_name)
    if not os.path.isfile(yaml_path):
        raise RuntimeError(
            f"Containerlab topology YAML not found: {yaml_path}. Regenerate topology before deploying Pingmesh."
        )

    with open(yaml_path, encoding="utf-8") as fh:
        topology = yaml.safe_load(fh) or {}
    linux_kind = ((topology.get("topology") or {}).get("kinds") or {}).get("linux") or {}
    binds = linux_kind.get("binds") or []
    if _PINGMESH_BIND not in binds:
        raise RuntimeError(
            f"Pingmesh bind missing from {yaml_path}: expected linux kind bind {_PINGMESH_BIND}. "
            "Regenerate topology before deploying Pingmesh."
        )


def _env_assignment(name: str, value: str | int | float | None) -> str:
    return f"{name}={shlex.quote(str(value or ''))}"


def _start_client_agent(
    *,
    client_name: str,
    container: str,
    topology_id: str,
    influxdb_url: str,
    influxdb_token: str,
    influxdb_org: str,
    influxdb_bucket: str,
) -> tuple[str, bool, str]:
    env_values: dict[str, str | int] = {
        "PYTHONPATH": f"{_CONTAINER_RUNTIME_DIR}:/tmp",
        "NETOPSBENCH_TOPOLOGY_ID": topology_id,
        "NETOPSBENCH_INFLUXDB_URL": influxdb_url,
        "NETOPSBENCH_INFLUXDB_TOKEN": influxdb_token,
        "NETOPSBENCH_INFLUXDB_ORG": influxdb_org,
        "NETOPSBENCH_INFLUXDB_BUCKET": influxdb_bucket,
    }
    env_block = " ".join(_env_assignment(name, value) for name, value in env_values.items())
    command = (
        "set -e; "
        "mkdir -p /var/log/pingmesh; "
        f"test -r {_CONTAINER_AGENT_SOURCE}; "
        f"test -r {_CONTAINER_PINGLIST}; "
        f"for pid in $(pgrep -f {shlex.quote(_CONTAINER_AGENT_MODULE)} || true); do "
        '[ "$pid" = "$$" ] && continue; '
        'kill "$pid" >/dev/null 2>&1 || true; '
        "done; "
        f"{env_block} nohup python3 -m {_CONTAINER_AGENT_MODULE} {_CONTAINER_PINGLIST} "
        "> /var/log/pingmesh/agent.log 2>&1 </dev/null &"
    )
    ret = _docker("exec", container, "sh", "-c", command, check=False, capture=True, timeout=30)
    if ret.returncode == 0:
        return client_name, True, ""

    detail = (ret.stderr or ret.stdout or "").strip() or f"docker exec exited with code {ret.returncode}"
    message = (
        "failed to start Pingmesh agent; expected /tmp/pingmesh bind and staged runtime to be readable. "
        "Regenerate the topology if the runtime files are missing."
        f" docker output: {detail}"
    )
    return client_name, False, message


def deploy_pingmesh(
    topology_dir: str,
    pinglist_file: str | None = None,
    influxdb_token: str | None = None,
    influxdb_org: str | None = None,
    influxdb_bucket: str | None = None,
    topology_id: str | None = None,
    verify: bool = False,
    parallelism: int = _DEFAULT_DEPLOY_PARALLELISM,
) -> DeployResult:
    """Deploy Pingmesh agents to every client container in *topology_dir*.

    Returns a :class:`DeployResult` with deployment counts and per-client
    verification status.
    """
    # --- resolve parameters from the canonical runtime topology ---
    pinglist_file = pinglist_file or _staged_pinglist_path(topology_dir)
    influxdb_token = influxdb_token or config.influxdb_token
    influxdb_org = influxdb_org or config.influxdb_org
    influxdb_bucket = influxdb_bucket or config.influxdb_bucket

    # --- load topology metadata ---
    topo = _load_topology_metadata(topology_dir)
    cycle_interval = topo.pingmesh.cycle_interval_seconds
    topology_id = topology_id or topo.topology_id
    lab_name = topo.name.strip()
    clients: list[str] = []
    for client in topo.clients():
        name = client.name.strip()
        if name:
            clients.append(name)

    if not clients:
        raise RuntimeError(f"No clients found in topology metadata: {topology_dir}/topology.json")
    _validate_pingmesh_bind(topology_dir, lab_name)

    # --- validate running containers ---
    logger.info("=== Deploying Pingmesh Agents ===")
    logger.info(f"Topology: {topology_dir}")
    logger.info(f"Topology ID: {topology_id}")
    logger.info(f"Pingmesh ingest: {_PINGMESH_INGEST_URL} bucket={influxdb_bucket}")
    logger.info(f"Cycle interval: {cycle_interval}s")
    deploy_parallelism = max(1, int(parallelism))
    logger.info(f"Deploy parallelism: {deploy_parallelism}")
    logger.info("")

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

    logger.info(f"[0/3] Validated {len(running_clients)}/{len(expected)} client containers")
    logger.info("")

    # --- stage runtime files and pinglist ---
    logger.info("[1/3] Staging Pingmesh runtime...")
    stage_started = time.monotonic()
    staged_pinglist = _stage_runtime(topology_dir, pinglist_file, topology_id)
    stage_elapsed = time.monotonic() - stage_started
    logger.info(f"  Runtime: {_runtime_dir(topology_dir)}")
    logger.info(f"  Pinglist: {staged_pinglist}")
    logger.info(f"  Staged in {stage_elapsed:.1f}s")
    logger.info("")

    # --- start agents in each client ---
    logger.info("[2/3] Starting agents in client containers...")
    result = DeployResult()
    start_started = time.monotonic()
    outcomes: dict[str, tuple[bool, str]] = {}
    futures = {}
    with ThreadPoolExecutor(max_workers=deploy_parallelism) as executor:
        for client_name in clients:
            container = clab_container_name(lab_name, client_name)
            if container not in running:
                outcomes[client_name] = (False, "container is not running")
                continue
            futures[
                executor.submit(
                    _start_client_agent,
                    client_name=client_name,
                    container=container,
                    topology_id=topology_id,
                    influxdb_url=_PINGMESH_INGEST_URL,
                    influxdb_token=influxdb_token,
                    influxdb_org=influxdb_org,
                    influxdb_bucket=influxdb_bucket,
                )
            ] = client_name

        for future in as_completed(futures):
            client_name, ok, message = future.result()
            outcomes[client_name] = (ok, message)

    for client_name in clients:
        container = clab_container_name(lab_name, client_name)
        ok, message = outcomes.get(client_name, (False, "not scheduled"))
        if ok:
            result.deployed += 1
        else:
            logger.error("%s: %s", container, message)
            result.failed.append(client_name)

    start_elapsed = time.monotonic() - start_started
    logger.info(f"  Started {result.deployed}/{len(clients)} clients in {start_elapsed:.1f}s")

    if result.failed:
        logger.warning(
            "%d client(s) failed: %s",
            len(result.failed),
            ", ".join(result.failed),
        )
    if result.deployed == 0:
        raise RuntimeError(
            "Pingmesh deployment failed for all clients; /tmp/pingmesh bind or staged runtime is not readable. "
            "Regenerate the topology before deploying Pingmesh."
        )

    # --- verify ---
    if verify:
        logger.info("")
        logger.info("[3/3] Verifying deployment...")
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
            agent_running = _CONTAINER_AGENT_MODULE in (ret.stdout or "")
            result.verified[client_name] = agent_running
            status = "✓ running" if agent_running else "✗ NOT running"
            logger.info(f"  {container}: Agent {status}")

    logger.info("")
    logger.info("=== Pingmesh Deployment Complete ===")
    logger.info(f"  Deployed to {result.deployed} clients")
    logger.info(f"  Pinglist: {staged_pinglist}")
    return result
