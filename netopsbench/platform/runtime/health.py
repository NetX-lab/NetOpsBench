"""Worker health check: container count, BGP convergence, connectivity, Pingmesh, observability.

Replaces ``scripts/runtime/check_worker_health.sh`` with a pure-Python
implementation providing structured error reporting and retry logic.

Programmatic usage::

    from netopsbench.platform.runtime.health import check_worker_health
    errors = check_worker_health(worker_identity)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from math import ceil
from pathlib import Path

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
    load_topology_manifest,
)
from netopsbench.platform.utils.control import OperationControl
from netopsbench.platform.utils.proc import docker_prefix, safe_run

logger = get_logger(__name__)

HEALTH_POLL_INTERVAL_SECONDS = 5
ACTIVE_INTERFACE_COVERAGE_MIN_RATIO = 0.5
_NETWORK_PROBE_SOURCE = (Path(__file__).parent.parent / "observability" / "_routing_probe.py").read_text(
    encoding="utf-8"
)


def _docker_exec(
    container: str,
    *cmd: str,
    check: bool = False,
    capture: bool = True,
    timeout: float = 60,
    control: OperationControl | None = None,
) -> subprocess.CompletedProcess:
    prefix = docker_prefix()
    return safe_run(
        [*prefix, "docker", "exec", container, *cmd],
        check=check,
        capture_output=capture,
        text=True,
        timeout=timeout,
        control=control,
    )


def _routing_state(
    container: str, control: OperationControl | None = None, route_snapshot: dict | None = None
) -> tuple[str, str]:
    budget = control.remaining(125) if control is not None else 125
    result = _docker_exec(
        container,
        "python3",
        "-c",
        _NETWORK_PROBE_SOURCE,
        "--budget-seconds",
        str(budget),
        timeout=budget,
        control=control,
    )
    if result.returncode != 0:
        return "", ""
    try:
        payload = json.loads(result.stdout)
        if payload["version"] != 1:
            return "", ""
        checks = payload["checks"]
        if route_snapshot is not None:
            fib = checks.get("fib", {})
            route_snapshot.update(
                packet_counters=payload.get("packet_counters", {"error": "Not collected"}),
                fib=json.loads(fib["stdout"]) if fib.get("returncode") == 0 else [],
                bgp_collection_ok=checks.get("bgp", {}).get("returncode") == 0,
                collected_at=time.time(),
            )
            if fib.get("returncode") != 0:
                route_snapshot["error"] = "FIB collection failed"
        outputs = []
        for name in ("bgp", "interfaces"):
            check = checks[name]
            if check["returncode"] != 0 or not isinstance(check["stdout"], str):
                return "", ""
            outputs.append(check["stdout"])
        return outputs[0], outputs[1]
    except (KeyError, TypeError, ValueError):
        return "", ""


def _running_container_count(lab_name: str, control: OperationControl | None = None) -> int:
    result = safe_run(
        [*docker_prefix(), "docker", "ps", "--filter", f"label=containerlab={lab_name}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        control=control,
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


def expected_network_counts(topo: TopologyManifest) -> dict[str, dict[str, int]]:
    """Compile interface and BGP expectations once from the current manifest."""
    routed = {item.name for item in topo.routing_devices()}
    counts = {item.name: {"active_interfaces": 0, "bgp_neighbors": 0} for item in topo.devices}
    for link in topo.links:
        routing_link = all(endpoint.device in routed for endpoint in link.endpoints)
        for endpoint in link.endpoints:
            counts[endpoint.device]["active_interfaces"] += 1
            counts[endpoint.device]["bgp_neighbors"] += int(routing_link)
    return counts


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
    *,
    deadline: float | None = None,
    cancel_event=None,
    route_snapshot: dict | None = None,
    expected_routes: dict | None = None,
    state_only: bool = False,
    recovery_audit: dict | None = None,
) -> list[str]:
    """Check health within one elapsed-time budget, including all retries."""
    started = time.monotonic()
    topo = _load_topology_metadata(str(worker.topology_dir))
    limit = started + get_scale_profile(topo.scale, scale_registry).health_timeout_seconds
    control = OperationControl(cancel_event, min(limit, deadline) if deadline is not None else limit)
    try:
        control.remaining()
        return _check_worker_health(
            worker,
            influxdb_url,
            influxdb_token,
            influxdb_org,
            health_retries,
            health_delay,
            scale_registry,
            require_client_agent,
            all_routing_devices,
            topology=topo,
            control=control,
            route_snapshot=route_snapshot,
            expected_routes=expected_routes,
            state_only=state_only,
            recovery_audit=recovery_audit,
        )
    except (TimeoutError, subprocess.TimeoutExpired):
        return ["Worker health check deadline exceeded"]


def _parallel_check(function, items, workers, control):
    """Queued probes must not start after cancellation or deadline expiry."""

    def probe(item):
        control.remaining()
        result = function(item)
        control.remaining()
        return result

    executor = ThreadPoolExecutor(max_workers=min(workers, max(1, len(items))))
    futures = []
    try:
        for item in items:
            control.remaining()
            futures.append(executor.submit(copy_context().run, probe, item))
        return [future.result(timeout=control.remaining()) for future in futures]
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def _check_worker_health(
    worker: RuntimeIdentity,
    influxdb_url: str | None = None,
    influxdb_token: str | None = None,
    influxdb_org: str | None = None,
    health_retries: int | None = None,
    health_delay: int | None = None,
    scale_registry: ScaleRegistry | None = None,
    require_client_agent: bool = True,
    all_routing_devices: bool = False,
    *,
    topology: TopologyManifest,
    control: OperationControl,
    route_snapshot: dict | None = None,
    expected_routes: dict | None = None,
    state_only: bool = False,
    recovery_audit: dict | None = None,
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
    topo = topology
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
        control=control,
    )
    if telegraf_container not in ret.stdout.strip().splitlines():
        errors.append(f"worker telegraf container is not running: {telegraf_container}")
        # Return early — remaining checks depend on the infra
        return errors

    # [2/5] Container count
    logger.info("[2/5] Checking container count...")
    expected_nodes = len(switches) + len(clients)
    running_nodes = _running_container_count(lab_name, control=control)
    if running_nodes < expected_nodes:
        errors.append(
            f"running node count mismatch for {lab_name}: got {running_nodes} expected at least {expected_nodes}"
        )
        return errors

    if state_only:
        names = [clab_container_name(lab_name, d.name) for d in topo.devices]
        inspected = safe_run(
            [
                *docker_prefix(),
                "docker",
                "inspect",
                "--format",
                "{{.Name}} {{.State.Running}} {{.State.OOMKilled}}",
                *names,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            control=control,
        )
        actual = {tuple(line.lstrip("/").split()) for line in inspected.stdout.splitlines()}
        if inspected.returncode or actual != {(name, "true", "false") for name in names}:
            return ["Runtime container identities or running state do not match"]

    bgp_device = routed[0].name if routed else "spine1"
    convergence_targets = _convergence_targets(topo, all_routing_devices=all_routing_devices)
    obs_device = edge_switches[0].name if edge_switches else (routed[0].name if routed else bgp_device)
    syslog_marker = f"NETOPSBENCH_HEALTH_{lab_name}_{time.time_ns()}"
    # Allow collector ingestion to overlap all-device convergence checks.
    if collector_ip and not state_only:
        _docker_exec(
            clab_container_name(lab_name, bgp_device),
            "bash",
            "-lc",
            f"logger -n '{collector_ip}' -P 514 -d '{syslog_marker}'",
            control=control,
        )
    network_interfaces: dict[str, set[str]] = {}
    expected_counts = expected_network_counts(topo)

    logger.info(
        "[3/5] Checking BGP and interface convergence on %s routing devices...",
        len(convergence_targets),
    )

    route_errors = {}

    def network_state(device) -> tuple[str, int, int, int, int]:
        container = clab_container_name(lab_name, device.name)
        if route_snapshot is None:
            bgp, interfaces = _routing_state(container, control=control)
        else:
            node_snapshot: dict = {}
            bgp, interfaces = _routing_state(container, control=control, route_snapshot=node_snapshot)
            from netopsbench.platform.observability.bgp_parser import parse_bgp_summary

            node_snapshot.update(bgp=parse_bgp_summary(bgp), interfaces=interfaces)
            route_snapshot.setdefault("nodes", {})[device.name] = node_snapshot
            if expected_routes is not None:
                from netopsbench.platform.observability.routes import verify_route_recovery

                route_errors[device.name] = verify_route_recovery(
                    {"nodes": {device.name: expected_routes["nodes"].get(device.name, {})}},
                    {"nodes": {device.name: node_snapshot}},
                )
        if state_only:
            from netopsbench.platform.observability.bgp_parser import parse_bgp_summary
            from netopsbench.platform.utils.interface_names import to_sonic_interface

            expected = (expected_routes or {}).get("nodes", {}).get(device.name)
            issues = route_errors.setdefault(device.name, [])
            if expected is None or expected.get("bgp_collection_ok") is not True:
                issues.append("Missing pre-injection BGP evidence")
            else:
                wanted = {
                    (row["neighbor"], row["asn"])
                    for row in expected.get("bgp", [])
                    if row.get("state") == "Established"
                }
                present = {
                    (row["neighbor"], row["asn"]) for row in parse_bgp_summary(bgp) if row.get("state") == "Established"
                }
                if wanted != present:
                    issues.append("BGP neighbor identities not restored")
            required_interfaces = {
                to_sonic_interface(e.interface)
                for link in topo.links
                for e in link.endpoints
                if e.device == device.name
            }
            if not required_interfaces.issubset(_parse_active_interfaces(interfaces)):
                issues.append("Expected interface identities not active")
        network_interfaces[device.name] = _parse_active_interfaces(interfaces)
        return (
            device.name,
            _parse_bgp_established(bgp),
            expected_counts[device.name]["bgp_neighbors"],
            len(network_interfaces[device.name]),
            expected_counts[device.name]["active_interfaces"],
        )

    pending: dict[str, tuple[int, int, int, int]] = {}
    pending_devices = list(convergence_targets)
    stable_passes = 0
    for attempt in range(retries):
        network_states = _parallel_check(network_state, pending_devices, 16, control)
        pending = {
            device: (established, expected_bgp, active, expected_active)
            for device, established, expected_bgp, active, expected_active in network_states
            if established < expected_bgp or active < expected_active or route_errors.get(device)
        }
        if recovery_audit is not None:
            recovery_audit["network_rounds"] = recovery_audit.get("network_rounds", 0) + 1
            recovery_audit["had_errors"] = recovery_audit.get("had_errors", False) or bool(pending)
        if not pending:
            stable_passes = stable_passes + 1 if len(pending_devices) == len(convergence_targets) else 0
            if not state_only or stable_passes == 2:
                break
            pending_devices = list(convergence_targets)
        else:
            stable_passes = 0
            pending_devices = [device for device in pending_devices if device.name in pending]
        if attempt + 1 < retries:
            control.sleep(delay)
    if state_only and stable_passes < 2 and not pending:
        return ["Insufficient stable network observations"]
    if pending:
        details = ", ".join(
            f"{device}(bgp={state[0]}/{state[1]},if={state[2]}/{state[3]})"
            for device, state in sorted(pending.items())[:12]
        )
        errors.append(f"network not converged on {len(pending)} routing devices: {details}")
        errors.extend(error for name in pending for error in route_errors.get(name, []))
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
        ret = _docker_exec(src_container, "ping", "-c", "1", "-W", "2", dst_ip, control=control)
        if ret.returncode == 0:
            connectivity_ok = True
            break
        if recovery_audit is not None:
            recovery_audit["had_errors"] = True
        control.sleep(delay)
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
                response = request_agent(management_ip, PINGMESH_CONTROL_PORT, "status", timeout=control.remaining(2))
            except (OSError, RuntimeError, ValueError):
                response = {}
            status = response.get("status") or {}
            heartbeat_ns = int(status.get("heartbeat_unix_ns", 0) or 0)
            heartbeat_age = time.time() - heartbeat_ns / 1_000_000_000
            return name, bool(
                response.get("ok") is True
                and status.get("ready") is True
                and status.get("plan_id") == topo.require_pingmesh_plan().settings.plan_id
                and status.get("config_schema_version") == 2
                and 0 <= heartbeat_age <= HEARTBEAT_MAX_AGE_SECONDS
            )

        pending_agents = list(agent_targets)
        failed_agents: list[str] = []
        for attempt in range(retries):
            agent_states = _parallel_check(agent_state, pending_agents, 32, control)
            failed_agents = [name for name, ready in agent_states if not ready]
            if not failed_agents:
                break
            if recovery_audit is not None:
                recovery_audit["had_errors"] = True
            failed_set = set(failed_agents)
            pending_agents = [client for client in pending_agents if str(client.get("name", "")).strip() in failed_set]
            if attempt + 1 < retries:
                control.sleep(delay)
        if failed_agents:
            preview = ", ".join(sorted(failed_agents)[:12])
            errors.append(f"Native Pingmesh process is not ready on {len(failed_agents)} clients: {preview}")
            return errors

    if state_only:
        if recovery_audit is not None:
            recovery_audit["collected_at"] = time.time()
        return errors

    # [5/5] InfluxDB observability path
    logger.info("[5/5] Checking InfluxDB observability path...")
    if obs_device in network_interfaces:
        observed_active_interfaces = sorted(network_interfaces[obs_device])
    else:
        ret = _docker_exec(
            clab_container_name(lab_name, obs_device), "bash", "-lc", "show interfaces status", control=control
        )
        observed_active_interfaces = sorted(_parse_active_interfaces(ret.stdout or ""))

    # Delegate to the observability validation module
    from netopsbench.platform.observability.influxdb import query_flux
    from netopsbench.platform.observability.validation import check_observability

    def query_runner(query: str) -> str:
        result = query_flux(
            influxdb_url, influxdb_token, influxdb_org, query, timeout=control.remaining(20), deadline=control.deadline
        )
        if result.status != "ok":
            raise RuntimeError(result.error or "InfluxDB query failed")
        return result.text

    pending_checks = {"interfaces", "syslog"}
    if require_client_agent:
        pending_checks.add("pingmesh")
    if bgp_device:
        pending_checks.add("bgp")

    def observe(check: str) -> tuple[str, list[str]]:
        try:
            result = check_observability(
                query_runner,
                bucket=worker.bucket,
                obs_device=obs_device,
                bgp_device=bgp_device,
                topology_id=worker.topology_id,
                plan_id=topo.require_pingmesh_plan().settings.plan_id,
                syslog_marker=syslog_marker,
                active_interfaces=observed_active_interfaces,
                min_active_coverage_ratio=ACTIVE_INTERFACE_COVERAGE_MIN_RATIO,
                require_pingmesh=require_client_agent,
                checks={check},
            )
        except RuntimeError as exc:
            result = [str(exc)]
        return check, result

    obs_errors: list[str] = []
    for attempt in range(retries):
        results = _parallel_check(observe, sorted(pending_checks), 4, control)
        obs_errors = [error for _check, check_errors in results for error in check_errors]
        pending_checks = {check for check, check_errors in results if check_errors}
        if not pending_checks:
            break
        if attempt + 1 < retries:
            control.sleep(delay)
    errors.extend(obs_errors)

    if not errors:
        logger.info("Worker health check passed: %s", lab_name)
    return errors


__all__ = ["check_worker_health"]
