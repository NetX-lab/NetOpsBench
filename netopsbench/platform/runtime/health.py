"""Worker health check: container count, BGP convergence, connectivity, Pingmesh, observability.

Replaces ``scripts/runtime/check_worker_health.sh`` with a pure-Python
implementation providing structured error reporting and retry logic.

Programmatic usage::

    from netopsbench.platform.runtime.health import check_worker_health
    errors = check_worker_health(worker_identity)
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from math import ceil

from netopsbench.config import config
from netopsbench.logging_utils import get_logger
from netopsbench.models.profiles import ScaleRegistry, get_scale_profile
from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.models.topology import DeviceRole, TopologyManifest
from netopsbench.platform.topology.topology_utils import (
    clab_container_name,
    coerce_topology_manifest,
    load_topology_manifest,
)
from netopsbench.platform.utils.proc import docker_prefix, safe_run

logger = get_logger(__name__)

HEALTH_POLL_INTERVAL_SECONDS = 5
ACTIVE_INTERFACE_COVERAGE_MIN_RATIO = 0.5


def _docker_exec(container: str, *cmd: str, check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    prefix = docker_prefix()
    return safe_run(
        [*prefix, "docker", "exec", container, *cmd],
        check=check,
        capture_output=capture,
        text=True,
        timeout=60,
    )


def _running_container_count(lab_name: str) -> int:
    result = safe_run(
        [*docker_prefix(), "docker", "ps", "--filter", f"label=containerlab={lab_name}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return len([line for line in result.stdout.strip().splitlines() if line])


def _load_topology_metadata(topology_dir: str) -> TopologyManifest:
    path = os.path.join(topology_dir, "topology.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"topology metadata not found: {path}")
    return load_topology_manifest(path)


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


def _parse_active_interfaces(show_interfaces_output: str) -> set[str]:
    active: set[str] = set()
    for line in show_interfaces_output.splitlines():
        parts = line.split()
        if not parts or not parts[0].startswith("Ethernet"):
            continue
        status_tokens = [part.lower() for part in parts[1:]]
        if status_tokens.count("up") >= 2:
            active.add(parts[0])
    return active


def _expected_active_interface_count(topo: TopologyManifest | dict, device: str) -> int:
    manifest = coerce_topology_manifest(topo)
    target = manifest.device(device)
    if target is None:
        return 0
    clients = manifest.clients()
    fat_tree_k = int(manifest.facts.fat_tree_k or 0)

    if fat_tree_k:
        half = fat_tree_k // 2
        if target.role is DeviceRole.CORE:
            return fat_tree_k
        if target.role is DeviceRole.AGG:
            return fat_tree_k
        if target.role is DeviceRole.EDGE:
            attached_clients = [client for client in clients if client.attached_switch == device]
            clients_per_edge = len(attached_clients) or int(manifest.facts.clients_per_attached_switch)
            return half + clients_per_edge

    if target.role is DeviceRole.SPINE:
        return int(manifest.facts.num_leafs or len(manifest.devices_by_role(DeviceRole.LEAF)))
    if target.role is DeviceRole.LEAF:
        num_spines = int(manifest.facts.num_spines or len(manifest.devices_by_role(DeviceRole.SPINE)))
        attached_clients = [client for client in clients if client.attached_switch == device]
        clients_per_leaf = len(attached_clients) or int(manifest.facts.clients_per_attached_switch)
        return num_spines + clients_per_leaf
    return 0


def _expected_bgp_neighbor_count(topo: TopologyManifest | dict, device: str) -> int:
    manifest = coerce_topology_manifest(topo)
    target = manifest.device(device)
    if target is None:
        return 0
    fat_tree_k = int(manifest.facts.fat_tree_k or 0)
    if fat_tree_k:
        if target.role in {DeviceRole.CORE, DeviceRole.AGG}:
            return fat_tree_k
        if target.role is DeviceRole.EDGE:
            return fat_tree_k // 2
    if target.role is DeviceRole.SPINE:
        return len(manifest.devices_by_role(DeviceRole.LEAF))
    if target.role is DeviceRole.LEAF:
        return len(manifest.devices_by_role(DeviceRole.SPINE))
    return 0


def _active_interface_coverage_error(
    container: str,
    device: str,
    active_interfaces: set[str],
    expected_count: int,
) -> str | None:
    if expected_count <= 0 or len(active_interfaces) >= expected_count:
        return None
    return (
        f"active interface coverage too low on {container}: "
        f"active={len(active_interfaces)} expected>={expected_count}"
    )


def check_worker_health(
    worker: RuntimeIdentity,
    influxdb_url: str | None = None,
    influxdb_token: str | None = None,
    influxdb_org: str | None = None,
    health_retries: int | None = None,
    health_delay: int | None = None,
    scale_registry: ScaleRegistry | None = None,
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
    topology_dir = str(worker.topology_dir)
    topo = _load_topology_metadata(topology_dir)
    projected_topo = topo.to_agent_topology()
    lab_name = worker.lab_name
    if topo.name != worker.lab_name or topo.topology_id != worker.topology_id:
        raise RuntimeError(
            "Runtime identity does not match topology manifest: "
            f"identity=({worker.lab_name}, {worker.topology_id}) "
            f"manifest=({topo.name}, {topo.topology_id})"
        )
    devices = projected_topo.get("devices", {}) or {}
    clients = devices.get("clients", []) or []
    switches = topo.switches()
    routed = topo.routing_devices()
    edge_switches = topo.edge_devices()
    collector_ip = topo.collector.ipv4.strip()

    influxdb_url = influxdb_url or config.influxdb_url
    influxdb_token = influxdb_token or config.influxdb_token
    influxdb_org = influxdb_org or config.influxdb_org

    profile = get_scale_profile(topo.scale, scale_registry)
    delay = HEALTH_POLL_INTERVAL_SECONDS if health_delay is None else health_delay
    retries = health_retries or max(1, ceil(profile.health_timeout_seconds / max(1, delay)))

    if len(clients) < 2:
        raise RuntimeError("need at least two clients in topology metadata for health check")

    logger.info("=== Worker Health Check ===")
    logger.info("Lab name: %s", lab_name)
    logger.info("Topology dir: %s", topology_dir)

    # [1/5] Worker telegraf
    logger.info("[1/5] Checking worker telegraf...")
    telegraf_container = f"telegraf-{lab_name}"
    ret = safe_run(
        [*docker_prefix(), "docker", "ps", "--format", "{{.Names}}"],
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
    expected_nodes = len(switches) + len(clients)
    running_nodes = _running_container_count(lab_name)
    if running_nodes < expected_nodes:
        errors.append(
            f"running node count mismatch for {lab_name}: " f"got {running_nodes} expected at least {expected_nodes}"
        )
        return errors

    # [3/5] BGP convergence
    bgp_device = routed[0].name if routed else "spine1"
    logger.info("[3/5] Checking BGP convergence on %s...", bgp_device)
    bgp_container = clab_container_name(lab_name, bgp_device)
    expected_bgp = _expected_bgp_neighbor_count(topo, bgp_device)
    established = 0
    for _ in range(retries):
        ret = _docker_exec(bgp_container, "vtysh", "-c", "show ip bgp summary")
        established = _parse_bgp_established(ret.stdout or "")
        if established >= expected_bgp:
            break
        time.sleep(delay)
    if established < expected_bgp:
        errors.append(f"BGP not converged on {bgp_container}: " f"established={established} expected>={expected_bgp}")
        return errors

    logger.info("[3b/5] Checking active interface coverage...")
    coverage_targets = [bgp_device]
    if topo.family == "fat-tree":
        for role_devices in (topo.devices_by_role(DeviceRole.AGG), edge_switches):
            if role_devices:
                name = role_devices[0].name
                if name and name not in coverage_targets:
                    coverage_targets.append(name)
        if edge_switches:
            last_edge = edge_switches[-1].name
            if last_edge and last_edge not in coverage_targets:
                coverage_targets.append(last_edge)
    elif edge_switches:
        first_edge = edge_switches[0].name
        if first_edge and first_edge not in coverage_targets:
            coverage_targets.append(first_edge)
        last_edge = edge_switches[-1].name
        if last_edge and last_edge not in coverage_targets:
            coverage_targets.append(last_edge)
    for device in coverage_targets:
        expected_count = _expected_active_interface_count(topo, device)
        if expected_count <= 0:
            continue
        container = clab_container_name(lab_name, device)
        active_ifaces: set[str] = set()
        for _ in range(retries):
            ret = _docker_exec(container, "bash", "-lc", "show interfaces status")
            active_ifaces = _parse_active_interfaces(ret.stdout or "")
            if len(active_ifaces) >= expected_count:
                break
            time.sleep(delay)
        coverage_error = _active_interface_coverage_error(
            container=container,
            device=device,
            active_interfaces=active_ifaces,
            expected_count=expected_count,
        )
        if coverage_error:
            errors.append(coverage_error)
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
        if "netopsbench.platform.pingmesh.cli" in (ret.stdout or ""):
            agent_running = True
            break
        time.sleep(delay)
    if not agent_running:
        errors.append(f"Pingmesh agent is not running in {src_container}")
        return errors

    # [5/5] InfluxDB observability path
    logger.info("[5/5] Checking InfluxDB observability path...")
    obs_device = edge_switches[0].name if edge_switches else (routed[0].name if routed else bgp_device)
    obs_container = clab_container_name(lab_name, obs_device)

    # Get active interfaces
    ret = _docker_exec(obs_container, "bash", "-lc", "show interfaces status")
    observed_active_interfaces = sorted(_parse_active_interfaces(ret.stdout or ""))

    # Send syslog marker
    syslog_marker = f"NETOPSBENCH_HEALTH_{lab_name}_{int(time.time())}"
    if collector_ip:
        _docker_exec(
            clab_container_name(lab_name, bgp_device),
            "bash",
            "-lc",
            f"logger -n '{collector_ip}' -P 514 -d '{syslog_marker}'",
        )

    # Delegate to the observability validation module
    from netopsbench.platform.observability.influxdb import query_flux
    from netopsbench.platform.observability.validation import check_observability

    def query_runner(query: str) -> str:
        result = query_flux(influxdb_url, influxdb_token, influxdb_org, query, timeout=20)
        if result.status != "ok":
            raise RuntimeError(result.error or "InfluxDB query failed")
        return result.text

    obs_errors: list[str] = []
    for attempt in range(retries):
        obs_errors = check_observability(
            query_runner,
            bucket=worker.bucket,
            obs_device=obs_device,
            bgp_device=bgp_device,
            topology_id=worker.topology_id,
            syslog_marker=syslog_marker,
            active_interfaces=observed_active_interfaces,
            min_active_coverage_ratio=ACTIVE_INTERFACE_COVERAGE_MIN_RATIO,
        )
        if not obs_errors:
            break
        if attempt + 1 < retries:
            time.sleep(delay)
    errors.extend(obs_errors)

    if not errors:
        logger.info("Worker health check passed: %s", lab_name)
    return errors


__all__ = ["check_worker_health"]
