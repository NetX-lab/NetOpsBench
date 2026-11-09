"""Measured lab capacity and resource monitoring, independent of acceptance policy."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from netopsbench.models.profiles import get_scale_profile
from netopsbench.platform.runtime.toolchain import runtime_backend_identity
from netopsbench.platform.topology.topology_utils import load_topology_manifest
from netopsbench.platform.utils.control import OperationControl
from netopsbench.platform.utils.proc import safe_run

GIB = 1024**3
EMERGENCY_MEMORY_BYTES = 12 * GIB


def available_memory_bytes() -> int:
    return next(
        int(line.split()[1]) * 1024
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemAvailable:")
    )


class ResourceMonitor:
    """Monitor memory before allocation; attach Docker sampling once a lab exists."""

    def __init__(self, result, cancel, emergency, lock):
        self.result, self.cancel, self.emergency, self.lock = result, cancel, emergency, lock
        self.target = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def watch(self, entry, lab_name, manifest):
        with self.lock:
            self.target = (entry, lab_name, manifest)

    def start(self):
        self._memory_sample()
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join()

    def _memory_sample(self):
        available = available_memory_bytes()
        with self.lock:
            self.result.setdefault("memory_samples", []).append(
                {
                    "time": datetime.now(UTC).isoformat(),
                    "available_bytes": available,
                }
            )
            if available < EMERGENCY_MEMORY_BYTES:
                self.result["resource_emergency"] = "MemAvailable below 12 GiB"
                self.emergency.set()
                self.cancel.set()

    def _run(self):
        # Docker stats may block; keep emergency monitoring on its own thread.
        sampler = threading.Thread(target=self._sample, daemon=True)
        sampler.start()
        try:
            while not self.stop.is_set():
                self._memory_sample()
                self.stop.wait(1)
        except Exception as exc:
            with self.lock:
                self.result["memory_monitor_error"] = str(exc)
            self.cancel.set()
        finally:
            sampler.join()

    def _sample(self):
        while not self.stop.is_set():
            with self.lock:
                target = self.target
            if target is not None and target[0] is not None:
                entry, lab_name, manifest = target
                try:
                    if isinstance(manifest, Path):
                        if not (manifest / "topology.json").exists():
                            self.stop.wait(1)
                            continue
                        manifest = load_topology_manifest(manifest)
                    sample = resource_snapshot(lab_name, manifest, control=OperationControl(self.stop))
                    with self.lock:
                        if self.target is target:
                            entry["resource_samples"].append(sample)
                except Exception as exc:
                    with self.lock:
                        if not self.stop.is_set():
                            entry["monitor_error"] = str(exc)
            self.stop.wait(15)


def memory_bytes(value: str) -> int:
    match = re.fullmatch(r"([\d.]+)([A-Za-z]+)", value.strip())
    if match is None:
        raise ValueError(f"Invalid Docker memory quantity: {value}")
    number, unit = match.groups()
    powers = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": GIB, "TiB": 1024**4}
    return int(float(number) * powers[unit])


def resource_snapshot(lab_name: str, manifest, *, control: OperationControl | None = None) -> dict:
    def output(command, **kwargs):
        return safe_run(command, check=True, control=control, **kwargs).stdout

    ports = Counter(endpoint.device for link in manifest.links for endpoint in link.endpoints)
    names = output(
        ["docker", "ps", "--filter", f"label=containerlab={lab_name}", "--format", "{{.Names}}"],
        text=True,
    ).split()
    services = output(["docker", "ps", "--format", "{{.Names}}"], text=True).split()
    services = [name for name in services if name in {"influxdb", "grafana", f"telegraf-{lab_name}"}]
    diagnostics = output(
        ["docker", "ps", "--filter", "label=netopsbench.role=diagnostic", "--format", "{{.Names}}"], text=True
    ).split()
    names.extend(services)
    names.extend(diagnostics)
    rows = (
        output(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", *names],
            text=True,
            timeout=60,
        )
        if names
        else ""
    )
    sizes = (
        output(
            ["docker", "inspect", "--size", "--format", "{{.Name}} {{.SizeRw}}", *names],
            text=True,
            timeout=60,
        )
        if names
        else ""
    )
    writable_bytes = {name.lstrip("/"): int(size) for name, size in map(str.split, sizes.splitlines())}
    return {
        "runtime_backend": runtime_backend_identity(),
        "time": datetime.now(UTC).isoformat(),
        "nodes": [
            dict(
                name=row["Name"],
                role=(
                    "diagnostic"
                    if row["Name"] in diagnostics
                    else (
                        "observability"
                        if row["Name"] in services
                        else "client" if "-client" in row["Name"] else "switch"
                    )
                ),
                data_ports=ports.get(row["Name"].removeprefix(f"clab-{lab_name}-"), 0),
                memory_bytes=memory_bytes(row["MemUsage"].split("/")[0]),
                writable_bytes=writable_bytes[row["Name"]],
            )
            for row in map(json.loads, rows.splitlines())
        ],
    }


def storage_snapshot() -> dict:
    """Measure shared observability data separately from container writable layers."""
    return {
        "time": datetime.now(UTC).isoformat(),
        "influxdb_data_bytes": int(
            subprocess.check_output(
                ["docker", "exec", "influxdb", "du", "-sb", "/var/lib/influxdb2"], text=True, timeout=60
            ).split()[0]
        ),
        "grafana_data_bytes": int(
            subprocess.check_output(
                ["docker", "exec", "grafana", "du", "-sb", "/var/lib/grafana"], text=True, timeout=60
            ).split()[0]
        ),
    }


def peak_sample(samples: list[dict]) -> dict:
    identities = [sample.get("runtime_backend") for sample in samples]
    if not identities or identities[0] is None or any(identity != identities[0] for identity in identities):
        raise ValueError("Capacity samples must belong to the same recorded runtime backend")
    nodes: dict[str, dict] = {}
    for sample in samples:
        for row in sample["nodes"]:
            peak = nodes.setdefault(row["name"], dict(row))
            for key in ("memory_bytes", "writable_bytes"):
                peak[key] = max(peak[key], row[key])
    return {"nodes": list(nodes.values()), "runtime_backend": identities[0]}


def capacity_check(
    sample: dict, scale: str, docker_root: Path, existing_runtime: bool = False, diagnostics: bool = True
) -> dict:
    if sample.get("runtime_backend") != runtime_backend_identity():
        raise ValueError("Capacity sample uses a different or unrecorded backend; measure the current runtime first")
    profile = get_scale_profile(scale)
    switches = [
        row for row in sample["nodes"] if row.get("role", "switch") == "switch" and "-client" not in row["name"]
    ]
    clients = [row["memory_bytes"] for row in sample["nodes"] if "-client" in row["name"]]
    if not switches or not clients:
        raise RuntimeError("Capacity check requires a converged switch/client sample")
    available = next(
        int(line.split()[1]) * 1024
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemAvailable:")
    )
    if profile.family == "clos":
        assert profile.num_leafs is not None and profile.num_spines is not None
        required_ports: Counter[int] = Counter()
        required_ports[int(profile.num_leafs)] += int(profile.num_spines)
        required_ports[int(profile.num_spines) + profile.clients_per_attached_switch] += int(profile.num_leafs)
    else:
        assert profile.fat_tree_k is not None and profile.num_cores is not None and profile.num_aggs is not None
        assert profile.num_edges is not None
        k = int(profile.fat_tree_k)
        required_ports = Counter()
        required_ports[k] += int(profile.num_cores) + int(profile.num_aggs)
        required_ports[k // 2 + profile.clients_per_attached_switch] += int(profile.num_edges)
    costs = []
    for ports, count in sorted(required_ports.items()):
        classes = sorted({row.get("data_ports", 0) for row in switches})
        representative = next((value for value in classes if value >= ports), classes[-1])
        rows = [row for row in switches if row.get("data_ports", 0) == representative]
        costs.append(
            {
                "required_ports": ports,
                "switches": count,
                "sample_ports": representative,
                "sample_memory_bytes": max(row["memory_bytes"] for row in rows),
                "sample_writable_bytes": max(row["writable_bytes"] for row in rows),
                "extrapolated": representative < ports,
            }
        )
    diagnostic_reserve = int(2.25 * GIB) if diagnostics else 0
    # Match port capacity before applying the maximum measured node cost and margin.
    estimated = int(
        1.2
        * (
            sum(row["sample_memory_bytes"] * row["switches"] for row in costs)
            + max(clients) * profile.total_clients
            + 8 * GIB
            + diagnostic_reserve
        )
    )
    disk_free = shutil.disk_usage(docker_root).free
    client_disk = max(row["writable_bytes"] for row in sample["nodes"] if "-client" in row["name"])
    estimated_disk = int(
        1.2
        * (
            sum(row["sample_writable_bytes"] * row["switches"] for row in costs)
            + client_disk * profile.total_clients
            + 20 * GIB
        )
    )
    full_memory, full_disk = estimated, estimated_disk
    if existing_runtime:
        # MemAvailable already excludes the retained lab's allocation. Resume
        # needs headroom for diagnosis/services, not a second copy of the lab.
        estimated = int(1.2 * (8 * GIB + diagnostic_reserve))
        estimated_disk = 20 * GIB
    result = {
        "available_memory_bytes": available,
        "estimated_memory_bytes": estimated,
        "docker_disk_free_bytes": disk_free,
        "estimated_disk_bytes": estimated_disk,
        "memory_margin": 1.2,
        "diagnostic_memory_reserve_bytes": diagnostic_reserve,
        "mode": "retained_runtime" if existing_runtime else "new_deployment",
        "full_runtime_estimated_memory_bytes": full_memory,
        "full_runtime_estimated_disk_bytes": full_disk,
        "switch_costs": costs,
        "status": (
            "passed"
            if estimated <= available
            and disk_free >= estimated_disk
            and not (not existing_runtime and scale == "fat-tree-k18" and any(row["extrapolated"] for row in costs))
            else "blocked"
        ),
    }
    return result


def require_capacity(capacity, allow):
    if capacity["status"] == "passed":
        return
    if not allow or (
        capacity["available_memory_bytes"] < capacity["estimated_memory_bytes"] / 1.2 + EMERGENCY_MEMORY_BYTES
        or capacity["docker_disk_free_bytes"] < capacity["estimated_disk_bytes"]
        or any(cost["extrapolated"] for cost in capacity["switch_costs"])
    ):
        raise RuntimeError(f"Capacity check failed: {capacity}")
