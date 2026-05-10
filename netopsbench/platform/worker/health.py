"""Worker health check: container count, BGP convergence, connectivity, Pingmesh, observability.

Replaces ``scripts/runtime/check_worker_health.sh`` with a pure-Python
implementation providing structured error reporting and retry logic.

CLI usage::

    python -m netopsbench.platform.worker.health <topology_dir>

Programmatic usage::

    from netopsbench.platform.worker.health import check_worker_health
    errors = check_worker_health("/path/to/topology_dir")
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence

from netopsbench.config import config
from netopsbench.logging_utils import get_logger
from netopsbench.platform.topology.topology_utils import clab_container_name
from netopsbench.platform.utils.proc import safe_run, sudo_prefix

logger = get_logger(__name__)


def _docker_exec(container: str, *cmd: str, check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    prefix = sudo_prefix()
    return safe_run(
        [*prefix, "docker", "exec", container, *cmd],
        check=check,
        capture_output=capture,
        text=True,
        timeout=60,
    )


def _running_container_count(lab_name: str) -> int:
    result = safe_run(
        [*sudo_prefix(), "docker", "ps", "--filter", f"label=containerlab={lab_name}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return len([line for line in result.stdout.strip().splitlines() if line])


def _load_topology_metadata(topology_dir: str) -> dict:
    path = os.path.join(topology_dir, "topology.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"topology metadata not found: {path}")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _parse_bgp_established(bgp_output: str) -> int:
    """Count established BGP sessions from ``vtysh -c 'show ip bgp summary'``."""
    count = 0
    for line in bgp_output.splitlines():
        parts = line.split()
        if not parts:
            continue
        # First field is neighbor IP (a.b.c.d), 10th field is state/pfxrcd (int if established)
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[0]) and len(parts) >= 10:
            try:
                int(parts[9])
                count += 1
            except ValueError:
                pass
    return count


def check_worker_health(
    topology_dir: str,
    influxdb_url: str | None = None,
    influxdb_token: str | None = None,
    influxdb_org: str | None = None,
    influxdb_bucket: str | None = None,
    health_retries: int | None = None,
    health_delay: int | None = None,
) -> list[str]:
    """Run all health checks and return a list of error messages (empty = healthy).

    Checks performed:
    1. Worker telegraf container is running
    2. Expected container count matches running containers
    3. BGP convergence on spine1
    4. Client-to-client connectivity + Pingmesh agent
    5. InfluxDB observability path (via validation module)
    """
    errors: list[str] = []
    topo = _load_topology_metadata(topology_dir)
    lab_name = (topo.get("name") or "dcn").strip()
    devices = topo.get("devices", {}) or {}
    spines = devices.get("spines", []) or []
    leafs = devices.get("leafs", []) or []
    clients = devices.get("clients", []) or []
    collector_ip = ((topo.get("collector", {}) or {}).get("ipv4") or "").strip()

    topology_id = config.topology_id or os.path.basename(topology_dir)
    influxdb_url = influxdb_url or config.influxdb_url
    influxdb_token = influxdb_token or config.influxdb_token
    influxdb_org = influxdb_org or config.influxdb_org

    telegraf_config = os.path.join(topology_dir, f"telegraf-{lab_name}.conf")
    if influxdb_bucket is None:
        influxdb_bucket = config.influxdb_bucket
    if not influxdb_bucket and os.path.isfile(telegraf_config):
        # Extract bucket from telegraf config (awk equivalent)
        with open(telegraf_config, encoding="utf-8") as fh:
            for line in fh:
                match = re.match(r'\s*bucket\s*=\s*"([^"]+)"', line)
                if match:
                    influxdb_bucket = match.group(1)
                    break
    influxdb_bucket = influxdb_bucket or "netopsbench"

    retries = health_retries or config.worker_health_retries
    delay = health_delay or config.worker_health_delay_seconds

    if len(clients) < 2:
        raise RuntimeError("need at least two clients in topology metadata for health check")

    logger.info("=== Worker Health Check ===")
    logger.info("Lab name: %s", lab_name)
    logger.info("Topology dir: %s", topology_dir)

    # [1/5] Worker telegraf
    logger.info("[1/5] Checking worker telegraf...")
    telegraf_container = f"telegraf-{lab_name}"
    ret = safe_run(
        [*sudo_prefix(), "docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if telegraf_container not in ret.stdout.strip().splitlines():
        errors.append(f"worker telegraf container is not running: {telegraf_container}")
        # Return early — remaining checks depend on the infra
        return errors

    # [2/5] Container count
    logger.info("[2/5] Checking container count...")
    expected_nodes = len(spines) + len(leafs) + len(clients)
    running_nodes = _running_container_count(lab_name)
    if running_nodes < expected_nodes:
        errors.append(
            f"running node count mismatch for {lab_name}: " f"got {running_nodes} expected at least {expected_nodes}"
        )
        return errors

    # [3/5] BGP convergence
    logger.info("[3/5] Checking BGP convergence on spine1...")
    spine_container = clab_container_name(lab_name, "spine1")
    leaf_count = len(leafs)
    established = 0
    for _ in range(retries):
        ret = _docker_exec(spine_container, "vtysh", "-c", "show ip bgp summary")
        established = _parse_bgp_established(ret.stdout or "")
        if established >= leaf_count:
            break
        time.sleep(delay)
    if established < leaf_count:
        errors.append(f"BGP not converged on {spine_container}: " f"established={established} expected>={leaf_count}")
        return errors

    # [4/5] Client connectivity + Pingmesh agent
    logger.info("[4/5] Checking client connectivity and Pingmesh agent...")
    src_client = clients[0]
    src_name = str(src_client.get("name", ""))
    src_leaf = str(src_client.get("leaf", ""))
    dst_ip = ""
    # Prefer cross-rack destination
    for other in clients[1:]:
        other_ip = str(other.get("data_ip", "")).strip()
        other_leaf = str(other.get("leaf", "")).strip()
        if other_leaf != src_leaf and other_ip:
            dst_ip = other_ip
            break
        if not dst_ip and other_ip:
            dst_ip = other_ip
    if not dst_ip:
        errors.append("could not determine destination client IP for health check")
        return errors

    src_container = clab_container_name(lab_name, src_name)
    connectivity_ok = False
    for _ in range(retries):
        ret = _docker_exec(src_container, "ping", "-c", "1", "-W", "2", dst_ip)
        if ret.returncode == 0:
            connectivity_ok = True
            break
        time.sleep(delay)
    if not connectivity_ok:
        errors.append(f"client connectivity failed from {src_container} to {dst_ip}")
        return errors

    agent_running = False
    for _ in range(retries):
        ret = _docker_exec(src_container, "ps", "aux")
        if "run_pingmesh_agent.py" in (ret.stdout or ""):
            agent_running = True
            break
        time.sleep(delay)
    if not agent_running:
        errors.append(f"Pingmesh agent is not running in {src_container}")
        return errors

    # [5/5] InfluxDB observability path
    logger.info("[5/5] Checking InfluxDB observability path...")
    obs_device = "leaf1" if leaf_count > 0 else "spine1"
    obs_container = clab_container_name(lab_name, obs_device)

    # Get active interfaces
    ret = _docker_exec(obs_container, "bash", "-lc", "show interfaces status")
    active_ifaces: list[str] = []
    for line in (ret.stdout or "").splitlines():
        parts = line.split()
        if parts and parts[0].startswith("Ethernet") and len(parts) >= 9:
            if parts[7].lower() == "up" and parts[8].lower() == "up":
                active_ifaces.append(parts[0])

    # Send syslog marker
    syslog_marker = f"NETOPSBENCH_HEALTH_{lab_name}_{int(time.time())}"
    if collector_ip:
        _docker_exec(
            clab_container_name(lab_name, "spine1"),
            "bash",
            "-lc",
            f"logger -n '{collector_ip}' -P 514 -d '{syslog_marker}'",
        )

    # Delegate to the observability validation module
    from netopsbench.platform.observability.validation import check_observability, run_query

    def query_runner(query: str) -> str:
        return run_query(influxdb_url, influxdb_token, influxdb_org, query)

    obs_errors = check_observability(
        query_runner,
        bucket=influxdb_bucket,
        obs_device=obs_device,
        bgp_device="spine1",
        topology_id=topology_id,
        syslog_marker=syslog_marker,
        active_interfaces=active_ifaces,
        min_active_coverage_ratio=config.active_interface_coverage_min_ratio,
    )
    errors.extend(obs_errors)

    if not errors:
        logger.info("Worker health check passed: %s", lab_name)
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point (backward-compatible with shell script args)."""
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        print("usage: python -m netopsbench.platform.worker.health <topology_dir>", file=sys.stderr)
        return 1

    topology_dir = args[0]
    try:
        errors = check_worker_health(topology_dir)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
