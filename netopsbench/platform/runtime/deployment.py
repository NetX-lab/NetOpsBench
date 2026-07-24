"""Python-owned Containerlab worker deployment and teardown."""

from __future__ import annotations

import fcntl
import ipaddress
import os
import signal
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from netopsbench.logging_utils import get_logger
from netopsbench.models.profiles import ScaleRegistry, get_scale_profile
from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.platform.runtime.apply_configs import apply_configs
from netopsbench.platform.topology.generator import generate_topology
from netopsbench.platform.topology.topology_utils import load_topology_manifest
from netopsbench.platform.utils.proc import docker_prefix, safe_run, sudo_prefix

APPLY_CONFIG_PARALLELISM = 32
LAB_REMOVAL_TIMEOUT_SECONDS = 120
LAB_REMOVAL_POLL_SECONDS = 1.0
RUNTIME_DEPLOY_LOCK_PATH = Path(tempfile.gettempdir()) / f"netopsbench-{os.getuid()}-runtime-deploy.lock"
logger = get_logger(__name__)


def management_subnet_stride(scale: str, registry: ScaleRegistry | None = None) -> int:
    prefix = get_scale_profile(scale, registry).management_prefix
    return 1 if prefix >= 24 else 2 ** (24 - prefix)


def management_subnet(scale: str, worker_index: int, registry: ScaleRegistry | None = None) -> str:
    profile = get_scale_profile(scale, registry)
    stride = management_subnet_stride(scale, registry)
    offset = worker_index if profile.management_prefix == 24 else (worker_index - 1) * stride
    third_octet = profile.management_subnet_base + offset
    if third_octet + stride - 1 > 254:
        raise RuntimeError(f"Worker index {worker_index} exceeds available management subnet range")
    return f"172.31.{third_octet}.0/{profile.management_prefix}"


def _docker_management_subnets() -> set[str]:
    docker = docker_prefix()
    listed = safe_run(
        [*docker, "docker", "network", "ls", "--format", "{{.ID}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if listed.returncode != 0:
        details = (listed.stderr or listed.stdout or "no diagnostic output").strip()
        raise RuntimeError(f"Unable to list Docker networks: {details}")
    network_ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if not network_ids:
        return set()
    inspected = safe_run(
        [
            *docker,
            "docker",
            "network",
            "inspect",
            "--format",
            "{{range .IPAM.Config}}{{println .Subnet}}{{end}}",
            *network_ids,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if inspected.returncode != 0:
        details = (inspected.stderr or inspected.stdout or "no diagnostic output").strip()
        raise RuntimeError(f"Unable to inspect Docker networks: {details}")
    return {line.strip() for line in inspected.stdout.splitlines() if line.strip()}


def allocate_management_subnets(scale: str, worker_count: int, registry: ScaleRegistry | None = None) -> list[str]:
    used_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for subnet in _docker_management_subnets():
        try:
            used_networks.append(ipaddress.ip_network(subnet, strict=False))
        except ValueError:
            continue

    stride = management_subnet_stride(scale, registry)
    start = int(management_subnet(scale, 1, registry).split(".")[2])
    selected: list[str] = []
    for octet in range(start, 255, stride):
        candidate = f"172.31.{octet}.0/{get_scale_profile(scale, registry).management_prefix}"
        candidate_network = ipaddress.ip_network(candidate, strict=False)
        selected_networks = [ipaddress.ip_network(item, strict=False) for item in selected]
        if any(
            candidate_network.version == network.version and candidate_network.overlaps(network)
            for network in [*used_networks, *selected_networks]
        ):
            continue
        selected.append(candidate)
        if len(selected) == worker_count:
            return selected
    raise RuntimeError(f"Unable to allocate {worker_count} unique management subnets for scale {scale}")


@contextmanager
def runtime_deploy_lock():
    """Serialize host-side network allocation and Containerlab creation."""
    with RUNTIME_DEPLOY_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def deploy_worker_lab(worker: RuntimeIdentity, scale: str, registry: ScaleRegistry | None = None) -> None:
    """Generate, deploy, and activate one worker without observability side effects."""
    topology_dir = Path(worker.topology_dir)
    topology_dir.mkdir(parents=True, exist_ok=True)
    stale_paths = [topology_dir / "configs", *topology_dir.glob("clab-*")]
    if stale_paths:
        safe_run(
            [*sudo_prefix(), "rm", "-rf", *(str(path) for path in stale_paths)],
            check=True,
            timeout=300,
        )

    generate_topology(
        scale=scale,
        output_dir=str(topology_dir),
        name=worker.lab_name,
        mgmt_subnet=worker.mgmt_subnet,
        mgmt_network=worker.mgmt_network,
        scale_registry=registry,
    )
    topology_file = topology_dir / f"{worker.lab_name}.clab.yaml"
    if not topology_file.is_file():
        raise FileNotFoundError(f"Generated Containerlab topology not found: {topology_file}")

    command = [*sudo_prefix(), "containerlab", "deploy", "-t", str(topology_file), "--reconfigure"]
    profile = get_scale_profile(scale, registry)
    if profile.containerlab_max_workers is not None:
        command.extend(["--max-workers", str(profile.containerlab_max_workers)])
    deploy_result = safe_run(command, cwd=topology_dir, check=False, timeout=profile.deploy_timeout_seconds)
    if deploy_result.returncode != 0:
        details = (deploy_result.stderr or deploy_result.stdout or "no diagnostic output").strip()
        raise RuntimeError(f"Containerlab deploy failed ({deploy_result.returncode}): {details[-4000:]}")

    result = apply_configs(str(topology_dir), APPLY_CONFIG_PARALLELISM, worker.lab_name)
    if result.failed:
        raise RuntimeError(f"SONiC activation failed for: {', '.join(result.failed)}")


def teardown_worker_lab(worker: RuntimeIdentity, registry: ScaleRegistry | None = None) -> None:
    """Remove one worker's collector, sidecar, Containerlab lab, and network."""
    topology_dir = Path(worker.topology_dir)
    _stop_collector(topology_dir / "bgp_collector.pid")
    docker = docker_prefix()
    safe_run([*docker, "docker", "rm", "-f", f"telegraf-{worker.lab_name}"], check=False, timeout=60)
    safe_run(
        [*docker, "docker", "network", "disconnect", worker.mgmt_network, "influxdb"],
        check=False,
        timeout=60,
    )

    topology_file = topology_dir / f"{worker.lab_name}.clab.yaml"
    manifest_file = topology_dir / "topology.json"
    profile = (
        get_scale_profile(load_topology_manifest(topology_dir).scale, registry) if manifest_file.is_file() else None
    )
    command = (
        [*sudo_prefix(), "containerlab", "destroy", "-t", str(topology_file), "--cleanup"]
        if topology_file.is_file()
        else [*sudo_prefix(), "containerlab", "destroy", "--name", worker.lab_name, "--cleanup"]
    )
    if profile is not None and profile.containerlab_max_workers is not None:
        command.extend(["--max-workers", str(profile.containerlab_max_workers)])
    teardown_timeout = profile.deploy_timeout_seconds if profile is not None else 600
    safe_run(command, cwd=topology_dir, check=False, timeout=teardown_timeout)

    names = _lab_container_names(docker, worker.lab_name)
    if names:
        safe_run([*docker, "docker", "rm", "-f", *names], check=False, timeout=300)
    _wait_for_lab_removal(docker, worker.lab_name)
    safe_run([*docker, "docker", "network", "rm", worker.mgmt_network], check=False, timeout=60)


def _lab_container_names(docker: list[str], lab_name: str) -> list[str]:
    result = safe_run(
        [
            *docker,
            "docker",
            "ps",
            "-a",
            "--filter",
            f"name=clab-{lab_name}-",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _wait_for_lab_removal(
    docker: list[str],
    lab_name: str,
    *,
    timeout: float = LAB_REMOVAL_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        names = _lab_container_names(docker, lab_name)
        if not names:
            return
        if time.monotonic() >= deadline:
            preview = ", ".join(names[:8])
            raise RuntimeError(f"Timed out waiting for lab containers to be removed: {preview}")
        safe_run([*docker, "docker", "rm", "-f", *names], check=False, timeout=60)
        time.sleep(LAB_REMOVAL_POLL_SECONDS)


def worker_from_cli(
    *,
    scale: str,
    topology_dir: str,
    lab_name: str,
    mgmt_subnet: str,
    bucket: str,
    mgmt_network: str | None = None,
    registry: ScaleRegistry | None = None,
) -> RuntimeIdentity:
    root = Path(topology_dir).resolve()
    profile = get_scale_profile(scale, registry)
    resolved_mgmt_subnet = mgmt_subnet or f"172.20.20.0/{profile.management_prefix}"
    return RuntimeIdentity.create(
        runtime_id=lab_name,
        worker_id="worker-1",
        worker_index=1,
        lab_name=lab_name,
        topology_dir=root,
        mgmt_subnet=resolved_mgmt_subnet,
        mgmt_network=mgmt_network or f"clab-mgmt-{lab_name}",
        bucket=bucket,
    )


def worker_from_topology(topology_dir: str) -> RuntimeIdentity:
    root = Path(topology_dir).expanduser().resolve()
    manifest = load_topology_manifest(root)
    return RuntimeIdentity.create(
        runtime_id=manifest.name,
        worker_id="worker-1",
        worker_index=1,
        lab_name=manifest.name,
        topology_dir=root,
        mgmt_subnet=manifest.management.ipv4_subnet,
        mgmt_network=manifest.management.network,
    )


def _stop_collector(pid_file: Path) -> None:
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(pid, signal.SIGTERM)
    except (FileNotFoundError, OSError, ValueError):
        pass
    pid_file.unlink(missing_ok=True)


__all__ = [
    "allocate_management_subnets",
    "deploy_worker_lab",
    "management_subnet",
    "runtime_deploy_lock",
    "teardown_worker_lab",
    "worker_from_cli",
    "worker_from_topology",
]
