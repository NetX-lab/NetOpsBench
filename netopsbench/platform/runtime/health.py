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
from concurrent.futures import ThreadPoolExecutor
from math import ceil

from netopsbench.config import config
from netopsbench.logging_utils import get_logger
from netopsbench.models.profiles import ScaleRegistry, get_scale_profile
from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.models.topology import Device, DeviceRole, TopologyManifest
from netopsbench.platform.client_agent.contract import (
    HEARTBEAT_MAX_AGE_SECONDS,
    PINGMESH_CONTROL_PORT,
)
from netopsbench.platform.client_agent.control import request_agent
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


def _convergence_targets(topo: TopologyManifest, *, all_routing_devices: bool) -> list[Device]:
    routed = topo.routing_devices()
    if all_routing_devices:
        return routed
    if not routed:
        return []
    edge_switches = topo.edge_devices()
    names = [routed[0].name]
    role_groups = (
        (topo.devices_by_role(DeviceRole.AGG), edge_switches) if topo.family == "fat-tree" else (edge_switches,)
    )
    for group in role_groups:
        if group:
            names.extend([group[0].name, group[-1].name])
    target_names = set(names)
    return [device for device in routed if device.name in target_names]


def check_worker_health(
    worker: RuntimeIdentity,
    influxdb_url: str | None = None,
    influxdb_token: str | None = None,
    influxdb_org: str | None = None,
    health_retries: int | None = None,
    health_delay: int | None = None,
    scale_registry: ScaleRegistry | None = None,
    require_client_agent: bool = True,
    all_routing_devices: bool = False,
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

    bgp_device = routed[0].name if routed else "spine1"
    convergence_targets = _convergence_targets(topo, all_routing_devices=all_routing_devices)

    logger.info(
        "[3/5] Checking BGP and interface convergence on %s routing devices...",
        len(convergence_targets),
    )

    def network_state(device) -> tuple[str, int, int, int, int]:
        container = clab_container_name(lab_name, device.name)
        bgp = _docker_exec(container, "vtysh", "-c", "show ip bgp summary")
        interfaces = _docker_exec(container, "bash", "-lc", "show interfaces status")
        return (
            device.name,
            _parse_bgp_established(bgp.stdout or ""),
            _expected_bgp_neighbor_count(topo, device.name),
            len(_parse_active_interfaces(interfaces.stdout or "")),
            _expected_active_interface_count(topo, device.name),
        )

    pending: dict[str, tuple[int, int, int, int]] = {}
    pending_devices = list(convergence_targets)
    for attempt in range(retries):
        with ThreadPoolExecutor(max_workers=min(16, max(1, len(pending_devices)))) as network_executor:
            network_states = list(network_executor.map(network_state, pending_devices))
        pending = {
            device: (established, expected_bgp, active, expected_active)
            for device, established, expected_bgp, active, expected_active in network_states
            if established < expected_bgp or active < expected_active
        }
        if not pending:
            break
        pending_devices = [device for device in pending_devices if device.name in pending]
        if attempt + 1 < retries:
            time.sleep(delay)
    if pending:
        details = ", ".join(
            f"{device}(bgp={state[0]}/{state[1]},if={state[2]}/{state[3]})"
            for device, state in sorted(pending.items())[:12]
        )
        errors.append(f"network not converged on {len(pending)} routing devices: {details}")
        return errors

    # [4/5] Client connectivity + native Pingmesh process
    logger.info("[4/5] Checking client connectivity and native Pingmesh process...")
    src_client = clients[0]
    src_name = str(src_client.get("name", ""))
    src_attached_switch = str(src_client.get("attached_switch", ""))
    dst_ip = ""
    # Prefer cross-rack destination
    for other in clients[1:]:
        other_ip = str(other.get("data_ip", "")).strip()
        other_attached_switch = str(other.get("attached_switch", "")).strip()
        if other_attached_switch != src_attached_switch and other_ip:
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

    if require_client_agent:
        agent_targets = clients if all_routing_devices else [src_client]

        def agent_state(client: dict) -> tuple[str, bool]:
            name = str(client.get("name", "")).strip()
            management_ip = str(client.get("mgmt_ip", "")).strip()
            if not management_ip:
                return name, False
            try:
                response = request_agent(management_ip, PINGMESH_CONTROL_PORT, "status")
            except (OSError, RuntimeError, ValueError):
                response = {}
            status = response.get("status") or {}
            heartbeat_ns = int(status.get("heartbeat_unix_ns", 0) or 0)
            heartbeat_age = time.time() - heartbeat_ns / 1_000_000_000
            return name, bool(
                response.get("ok") is True
                and status.get("ready") is True
                and 0 <= heartbeat_age <= HEARTBEAT_MAX_AGE_SECONDS
            )

        pending_agents = list(agent_targets)
        failed_agents: list[str] = []
        for attempt in range(retries):
            with ThreadPoolExecutor(max_workers=min(32, max(1, len(pending_agents)))) as agent_executor:
                agent_states = list(agent_executor.map(agent_state, pending_agents))
            failed_agents = [name for name, ready in agent_states if not ready]
            if not failed_agents:
                break
            failed_set = set(failed_agents)
            pending_agents = [client for client in pending_agents if str(client.get("name", "")).strip() in failed_set]
            if attempt + 1 < retries:
                time.sleep(delay)
        if failed_agents:
            preview = ", ".join(sorted(failed_agents)[:12])
            errors.append(f"Native Pingmesh process is not ready on {len(failed_agents)} clients: {preview}")
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
            require_pingmesh=require_client_agent,
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
