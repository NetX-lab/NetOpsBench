"""Fast parallel SONiC configuration application.

Replaces ``scripts/runtime/apply_configs_fast.sh`` with a pure-Python
implementation using :class:`concurrent.futures.ThreadPoolExecutor`
instead of GNU ``parallel``, removing that external dependency.

CLI usage::

    python -m netopsbench.platform.runtime.apply_configs <topology_dir> [max_parallel] [lab_name]

Programmatic usage::

    from netopsbench.platform.runtime.apply_configs import apply_configs
    result = apply_configs("/path/to/topology_dir", max_parallel=4)
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from netopsbench.config import config
from netopsbench.platform.topology.topology_utils import clab_container_name
from netopsbench.platform.utils.proc import safe_run, sudo_prefix


@dataclass
class ApplyResult:
    """Summary of configuration application."""

    succeeded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0


def _resolve_lab_name(topology_dir: str, lab_name: str | None) -> str:
    """Resolve the lab name from argument, topology metadata, or clab YAML."""
    if lab_name:
        return lab_name

    metadata_file = os.path.join(topology_dir, "topology.json")
    if os.path.isfile(metadata_file):
        with open(metadata_file, encoding="utf-8") as fh:
            data = json.load(fh)
        name = (data.get("name") or "").strip()
        if name:
            return name

    # Fall back to parsing the clab YAML header
    clab_files = sorted(glob.glob(os.path.join(topology_dir, "*.clab.y*ml")))
    for clab_file in clab_files:
        with open(clab_file, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                    if name:
                        return name
                    break

    return "dcn"


def _wait_for_sonic(container: str, max_tries: int | None = None, delay: int = 5) -> bool:
    """Block until SONiC ``vtysh`` is responsive or *max_tries* is exceeded."""
    max_tries = max_tries or config.sonic_wait_tries
    prefix = sudo_prefix()
    for _ in range(max_tries):
        try:
            ret = safe_run(
                [*prefix, "docker", "exec", container, "vtysh", "-c", "show version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            time.sleep(delay)
            continue
        if ret.returncode == 0:
            return True
        time.sleep(delay)
    return False


def _apply_single_device(
    device: str,
    topology_dir: str,
    lab_name: str,
    max_attempts: int = 4,
) -> tuple[str, bool, str]:
    """Apply config to one device. Returns ``(device, success, message)``."""
    config_file = os.path.join(topology_dir, "configs", f"{device}.sh")
    container = clab_container_name(lab_name, device)
    prefix = sudo_prefix()

    if not os.path.isfile(config_file):
        return (device, False, f"config file not found: {config_file}")

    if not _wait_for_sonic(container):
        return (device, False, "SONiC services not ready")

    # Copy config into container
    safe_run(
        [*prefix, "docker", "cp", config_file, f"{container}:/tmp/config.sh"],
        capture_output=True,
        check=False,
        timeout=60,
    )

    for attempt in range(1, max_attempts + 1):
        ret = safe_run(
            [*prefix, "docker", "exec", container, "bash", "/tmp/config.sh"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if ret.returncode == 0:
            # Enable L4-aware ECMP hashing so that multi-flow probes
            # (UDP/TCP with varying src_port) actually fan out across
            # parallel uplinks. Without this, Linux defaults to L3-only
            # hashing (src_ip, dst_ip) and every flow between the same
            # endpoint pair pins to a single ECMP nexthop — causing
            # link-level faults on alternate paths to be invisible to
            # edge probes. See net.ipv4.fib_multipath_hash_policy(7).
            safe_run(
                [*prefix, "docker", "exec", container, "sysctl", "-w", "net.ipv4.fib_multipath_hash_policy=1"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            return (device, True, "config applied")
        if attempt < max_attempts:
            print(f"  ! [{device}] attempt {attempt}/{max_attempts} failed; retrying...", flush=True)
            time.sleep(5)

    return (device, False, "failed to apply config after all attempts")


def apply_configs(
    topology_dir: str,
    max_parallel: int = 4,
    lab_name: str | None = None,
) -> ApplyResult:
    """Apply SONiC configs for all spine/leaf devices in *topology_dir*.

    Uses :class:`ThreadPoolExecutor` for parallelism, replacing the
    previous dependency on GNU ``parallel``.
    """
    lab_name = _resolve_lab_name(topology_dir, lab_name)

    # Discover devices
    devices: list[str] = []
    for pattern in ("spine*.sh", "leaf*.sh"):
        for cfg in sorted(glob.glob(os.path.join(topology_dir, "configs", pattern))):
            devices.append(Path(cfg).stem)

    if not devices:
        raise RuntimeError(f"No config files found in {topology_dir}/configs/")

    print("=== Fast SONiC Configuration Application ===")
    print(f"Topology directory: {topology_dir}")
    print(f"Max parallel: {max_parallel}")
    print(f"Lab name: {lab_name}")
    print()
    print(f"Found {len(devices)} devices to configure: {' '.join(devices)}")
    print()
    print("[1/2] Applying configurations in parallel...")

    start = time.monotonic()
    result = ApplyResult()

    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        futures = {executor.submit(_apply_single_device, device, topology_dir, lab_name): device for device in devices}
        for future in as_completed(futures):
            device, success, msg = future.result()
            if success:
                print(f"  ok [{device}] {msg}")
                result.succeeded.append(device)
            else:
                print(f"  x  [{device}] {msg}")
                result.failed.append(device)

    result.elapsed_seconds = time.monotonic() - start
    print()
    print(f"Applied configs in {result.elapsed_seconds:.0f}s")
    print()

    # [2/2] Quick control-plane verification
    print("[2/2] Quick control-plane verification...")
    prefix = sudo_prefix()
    for role in ("spine", "leaf"):
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
    print("=== Configuration Application Complete ===")
    if result.failed:
        print(f"WARNING: {len(result.failed)} device(s) failed: {', '.join(result.failed)}")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point (backward-compatible with shell script args)."""
    args = list(argv if argv is not None else sys.argv[1:])

    topology_dir = args[0] if len(args) > 0 else "generated_topology"
    max_parallel_str = args[1] if len(args) > 1 else "4"
    max_parallel = int(max_parallel_str) if max_parallel_str else 4
    lab_name = args[2] if len(args) > 2 else None

    try:
        result = apply_configs(topology_dir, max_parallel, lab_name)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
