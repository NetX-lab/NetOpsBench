"""Post-deploy SONiC activation for preseeded startup artifacts.

Programmatic usage::

    from netopsbench.platform.runtime.apply_configs import apply_configs
    result = apply_configs("/path/to/topology_dir", max_parallel=32)
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from netopsbench.models.topology import DeviceRole, TopologyManifest
from netopsbench.platform.topology.config import SONIC_PORT_COUNTER_INTERVAL_MS
from netopsbench.platform.topology.topology_utils import clab_container_name, load_topology_manifest
from netopsbench.platform.utils.proc import docker_prefix, safe_run

_REQUIRED_SONIC_SERVICES = ("redis-server", "orchagent", "zebra")
SONIC_READINESS_MAX_TRIES = 180


@dataclass
class ApplyResult:
    """Summary of configuration application."""

    succeeded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    durations: dict[str, float] = field(default_factory=dict)
    readiness_durations: dict[str, float] = field(default_factory=dict)
    activation_durations: dict[str, float] = field(default_factory=dict)
    elapsed_seconds: float = 0.0


def _preseed_config_file(topology_dir: str, device: str) -> str:
    return os.path.join(topology_dir, "configs", "sonic", device, "config_db.json")


def _expected_port_count(config_file: str) -> int:
    try:
        with open(config_file, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return 0
    ports = payload.get("PORT")
    return len(ports) if isinstance(ports, dict) else 0


def _expected_interface_addresses(config_file: str) -> dict[str, set[str]]:
    """Return interface addresses declared by the preseeded ConfigDB."""
    try:
        with open(config_file, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    interfaces = payload.get("INTERFACE")
    if not isinstance(interfaces, dict):
        return {}

    expected: dict[str, set[str]] = {}
    for key in interfaces:
        if not isinstance(key, str) or "|" not in key:
            continue
        interface, address = key.split("|", 1)
        if interface and address:
            expected.setdefault(interface, set()).add(address)
    return expected


def _startup_wrapper_file(topology_dir: str) -> str:
    return os.path.join(topology_dir, "configs", "sonic", "start.sh")


def _device_sort_key(device: str) -> tuple[int, int | str]:
    for rank, prefix in enumerate(("core", "agg", "edge", "spine", "leaf")):
        if device.startswith(prefix):
            suffix = device[len(prefix) :]
            return (rank, int(suffix) if suffix.isdigit() else suffix)
    return (5, device)


def _discover_devices(topology_dir: str) -> list[str]:
    preseed_root = Path(topology_dir) / "configs" / "sonic"
    if not preseed_root.exists():
        return []
    devices = [
        device_dir.name
        for device_dir in preseed_root.iterdir()
        if device_dir.is_dir() and (device_dir / "config_db.json").is_file()
    ]
    return sorted(devices, key=_device_sort_key)


def _ecmp_hash_policies(manifest: TopologyManifest, devices: list[str]) -> dict[str, int]:
    policies: dict[str, int] = {}
    for device in devices:
        manifest_device = manifest.device(device)
        if manifest_device is None or manifest_device.role is DeviceRole.CLIENT:
            raise RuntimeError(f"Preseeded SONiC device {device!r} is missing from topology manifest switches")
        policies[device] = manifest.routing.ecmp_hash_policy_by_role[manifest_device.role]
    return policies


def _configdb_ready(prefix: list[str], container: str) -> bool:
    try:
        configdb_ret = safe_run(
            [
                *prefix,
                "docker",
                "exec",
                container,
                "sonic-cfggen",
                "-d",
                "-v",
                "DEVICE_METADATA.localhost.hostname",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False
    return configdb_ret.returncode == 0


def _startup_status(prefix: list[str], container: str) -> str | None:
    try:
        ret = safe_run(
            [*prefix, "docker", "exec", container, "supervisorctl", "status", "start.sh"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return None
    parts = str(ret.stdout or ret.stderr or "").split()
    return parts[1].upper() if len(parts) >= 2 else None


def _required_services_ready(prefix: list[str], container: str) -> bool:
    try:
        ret = safe_run(
            [*prefix, "docker", "exec", container, "supervisorctl", "status", *_REQUIRED_SONIC_SERVICES],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False
    output = str(ret.stdout or ret.stderr or "")
    states: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1].upper()
    return all(states.get(service) == "RUNNING" for service in _REQUIRED_SONIC_SERVICES)


def _countersdb_ready(prefix: list[str], container: str, expected_port_count: int) -> bool:
    try:
        ret = safe_run(
            [
                *prefix,
                "docker",
                "exec",
                container,
                "redis-cli",
                "-n",
                "2",
                "--raw",
                "HLEN",
                "COUNTERS_PORT_NAME_MAP",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False
    if ret.returncode != 0:
        return False
    try:
        return int(str(ret.stdout).strip()) >= expected_port_count
    except ValueError:
        return False


def _wait_for_sonic(
    device: str,
    container: str,
    max_tries: int | None = None,
    delay: int = 5,
    expected_port_count: int | None = None,
) -> bool:
    """Block until SONiC control-plane and ConfigDB commands are responsive."""
    max_tries = max_tries or SONIC_READINESS_MAX_TRIES
    prefix = docker_prefix()
    for _ in range(max_tries):
        _startup_status(prefix, container)
        configdb_ready = _configdb_ready(prefix, container)
        if not configdb_ready:
            time.sleep(delay)
            continue

        try:
            vtysh_ret = safe_run(
                [*prefix, "docker", "exec", container, "vtysh", "-c", "show version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            time.sleep(delay)
            continue

        if vtysh_ret.returncode != 0:
            time.sleep(delay)
            continue

        if not _required_services_ready(prefix, container):
            time.sleep(delay)
            continue

        if _configdb_ready(prefix, container) and (
            expected_port_count is None or _countersdb_ready(prefix, container, expected_port_count)
        ):
            return True
        time.sleep(delay)
    return False


def _activate_preseed_device(prefix: list[str], container: str, ecmp_hash_policy: int) -> bool:
    command = (
        "set -e; "
        f"sysctl -w net.ipv4.fib_multipath_hash_policy={ecmp_hash_policy} >/dev/null; "
        f'test "$(sysctl -n net.ipv4.fib_multipath_hash_policy)" = "{ecmp_hash_policy}"; '
        "supervisorctl start bgpd >/dev/null 2>&1 || true; "
        "if [ -s /etc/frr/frr.conf ]; then "
        "vtysh -b >/tmp/netopsbench-vtysh-batch.log 2>&1; "
        "fi; "
        f"counterpoll port interval {SONIC_PORT_COUNTER_INTERVAL_MS} >/dev/null; "
        "mkdir -p /var/run/telemetry /var/log/telemetry || true; "
        "pkill -x telemetry >/dev/null 2>&1 || true; "
        "for _ in $(seq 1 50); do "
        "pgrep -x telemetry >/dev/null 2>&1 || break; sleep 0.1; "
        "done; "
        "if pgrep -x telemetry >/dev/null 2>&1; then exit 1; fi; "
        "nohup /usr/sbin/telemetry -port 50051 -noTLS -client_auth none "
        ">/var/log/telemetry/telemetry.log 2>&1 </dev/null & "
        "for _ in $(seq 1 50); do "
        "pgrep -x telemetry >/dev/null 2>&1 && exit 0; sleep 0.1; "
        "done; exit 1"
    )
    ret = safe_run(
        [*prefix, "docker", "exec", container, "bash", "-lc", command],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return ret.returncode == 0


def _kernel_interface_addresses(prefix: list[str], container: str) -> dict[str, set[str]] | None:
    try:
        ret = safe_run(
            [*prefix, "docker", "exec", container, "ip", "-j", "address", "show"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return None
    if ret.returncode != 0:
        return None
    try:
        payload = json.loads(ret.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None

    observed: dict[str, set[str]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        interface = item.get("ifname")
        if not isinstance(interface, str):
            continue
        for address in item.get("addr_info") or []:
            if not isinstance(address, dict):
                continue
            local = address.get("local")
            prefixlen = address.get("prefixlen")
            if isinstance(local, str) and isinstance(prefixlen, int):
                observed.setdefault(interface, set()).add(f"{local}/{prefixlen}")
    return observed


def _reconcile_preseed_interfaces(prefix: list[str], container: str, config_file: str) -> list[str]:
    """Repair rare SONiC-VS startup drift between ConfigDB and kernel state."""
    expected = _expected_interface_addresses(config_file)
    if not expected:
        return []
    observed = _kernel_interface_addresses(prefix, container)
    if observed is None:
        return ["unable to inspect kernel interface addresses"]

    missing = sorted(
        (interface, address)
        for interface, addresses in expected.items()
        for address in addresses
        if address not in observed.get(interface, set())
    )
    for interface, address in missing:
        for command in (
            ["ip", "link", "set", "dev", interface, "up"],
            ["ip", "address", "replace", address, "dev", interface],
        ):
            try:
                ret = safe_run(
                    [*prefix, "docker", "exec", container, *command],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                )
            except subprocess.TimeoutExpired:
                return [f"{interface}|{address}: command timed out"]
            if ret.returncode != 0:
                detail = (ret.stderr or ret.stdout or "").strip()
                return [f"{interface}|{address}: {detail or f'exit status {ret.returncode}'}"]

    observed = _kernel_interface_addresses(prefix, container)
    if observed is None:
        return ["unable to verify kernel interface addresses"]
    return sorted(
        f"{interface}|{address}"
        for interface, addresses in expected.items()
        for address in addresses
        if address not in observed.get(interface, set())
    )


def _apply_single_device(
    device: str,
    topology_dir: str,
    lab_name: str,
    ecmp_hash_policy: int,
) -> tuple[str, bool, str, float, float, float]:
    """Activate one preseeded device after Containerlab has started it."""
    started_at = time.monotonic()
    container = clab_container_name(lab_name, device)
    prefix = docker_prefix()
    preseed_config = _preseed_config_file(topology_dir, device)
    startup_wrapper = _startup_wrapper_file(topology_dir)

    if not os.path.isfile(preseed_config):
        return (device, False, f"preseed config not found: {preseed_config}", time.monotonic() - started_at, 0.0, 0.0)

    if not os.path.isfile(startup_wrapper):
        return (
            device,
            False,
            f"startup wrapper not found: {startup_wrapper}; regenerate topology before deploying",
            time.monotonic() - started_at,
            0.0,
            0.0,
        )

    expected_port_count = _expected_port_count(preseed_config)
    if expected_port_count <= 0:
        return (
            device,
            False,
            f"preseed config has no PORT entries: {preseed_config}",
            time.monotonic() - started_at,
            0.0,
            0.0,
        )

    readiness_started = time.monotonic()
    if not _wait_for_sonic(device, container, expected_port_count=expected_port_count):
        readiness_elapsed = time.monotonic() - readiness_started
        return (
            device,
            False,
            f"SONiC services not ready after {readiness_elapsed:.1f}s",
            time.monotonic() - started_at,
            readiness_elapsed,
            0.0,
        )
    readiness_elapsed = time.monotonic() - readiness_started

    activation_started = time.monotonic()
    if _activate_preseed_device(prefix, container, ecmp_hash_policy):
        unreconciled = _reconcile_preseed_interfaces(prefix, container, preseed_config)
        activation_elapsed = time.monotonic() - activation_started
        if not unreconciled:
            return (
                device,
                True,
                "post-deploy activation",
                time.monotonic() - started_at,
                readiness_elapsed,
                activation_elapsed,
            )
        return (
            device,
            False,
            "post-deploy interface reconciliation failed: " + ", ".join(unreconciled),
            time.monotonic() - started_at,
            readiness_elapsed,
            activation_elapsed,
        )
    activation_elapsed = time.monotonic() - activation_started
    return (
        device,
        False,
        "post-deploy activation failed",
        time.monotonic() - started_at,
        readiness_elapsed,
        activation_elapsed,
    )


def apply_configs(
    topology_dir: str,
    max_parallel: int = 32,
    lab_name: str | None = None,
) -> ApplyResult:
    """Activate all preseeded SONiC devices in *topology_dir*."""
    manifest = load_topology_manifest(topology_dir)
    lab_name = lab_name or manifest.name

    # Discover devices
    devices = _discover_devices(topology_dir)

    if not devices:
        raise RuntimeError(f"No preseed config_db.json files found in {topology_dir}/configs/sonic/")

    ecmp_hash_policies = _ecmp_hash_policies(manifest, devices)

    print("=== SONiC Post-Deploy Activation ===")
    print(f"Topology directory: {topology_dir}")
    print(f"Max parallel: {max_parallel}")
    print(f"Lab name: {lab_name}")
    print()
    print(f"Found {len(devices)} devices to configure: {' '.join(devices)}")
    print()
    print("[1/2] Activating devices in parallel...")

    start = time.monotonic()
    result = ApplyResult()

    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        futures = {
            executor.submit(
                _apply_single_device,
                device,
                topology_dir,
                lab_name,
                ecmp_hash_policies[device],
            ): device
            for device in devices
        }
        for future in as_completed(futures):
            device, success, msg, elapsed, readiness_elapsed, activation_elapsed = future.result()
            result.durations[device] = elapsed
            result.readiness_durations[device] = readiness_elapsed
            result.activation_durations[device] = activation_elapsed
            if success:
                print(
                    f"  ok [{device}] {msg}; ready={readiness_elapsed:.1f}s "
                    f"activate={activation_elapsed:.1f}s total={elapsed:.1f}s"
                )
                result.succeeded.append(device)
            else:
                print(
                    f"  x  [{device}] {msg}; ready={readiness_elapsed:.1f}s "
                    f"activate={activation_elapsed:.1f}s total={elapsed:.1f}s"
                )
                result.failed.append(device)

    result.elapsed_seconds = time.monotonic() - start
    print()
    print(f"Activated devices in {result.elapsed_seconds:.0f}s")
    if result.durations:
        slowest = sorted(result.durations.items(), key=lambda item: item[1], reverse=True)[:10]
        slowest_text = ", ".join(f"{device}={elapsed:.1f}s" for device, elapsed in slowest)
        print(f"Slowest devices: {slowest_text}")
        slowest_ready = sorted(result.readiness_durations.items(), key=lambda item: item[1], reverse=True)[:10]
        slowest_ready_text = ", ".join(f"{device}={elapsed:.1f}s" for device, elapsed in slowest_ready)
        print(f"Slowest readiness: {slowest_ready_text}")
        slowest_activation = sorted(result.activation_durations.items(), key=lambda item: item[1], reverse=True)[:10]
        slowest_activation_text = ", ".join(f"{device}={elapsed:.1f}s" for device, elapsed in slowest_activation)
        print(f"Slowest activation: {slowest_activation_text}")
    print()

    # [2/2] Quick control-plane verification
    print("[2/2] Quick control-plane verification...")
    prefix = docker_prefix()
    for role in ("core", "agg", "edge", "spine", "leaf"):
        candidates = [d for d in devices if d.startswith(role)]
        if candidates:
            container = clab_container_name(lab_name, candidates[0])
            safe_run(
                [*prefix, "docker", "exec", container, "vtysh", "-c", "show ip bgp summary"],
                capture_output=True,
                check=False,
                timeout=30,
            )
            print(f"  checked {candidates[0]}")

    print()
    print("=== SONiC Activation Complete ===")
    if result.failed:
        print(f"WARNING: {len(result.failed)} device(s) failed: {', '.join(result.failed)}")
    return result
