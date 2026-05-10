"""Shared topology helpers used by runtime and tooling components."""

from __future__ import annotations

import copy
import glob
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from netopsbench.config import config
from netopsbench.logging_utils import get_logger
from netopsbench.platform.utils.proc import safe_run, sudo_prefix

logger = get_logger(__name__)


def clab_container_name(lab_name: str, device_name: str) -> str:
    """Return the Containerlab container name for a device."""
    return f"clab-{lab_name}-{device_name}"


@dataclass
class TopologyState:
    """Normalized topology state shared by the injector and agent toolkit."""

    topology_name: str = "dcn"
    container_names: dict[str, str] = field(default_factory=dict)
    topology_metadata: dict[str, Any] = field(default_factory=dict)
    device_mgmt_ips: dict[str, str] = field(default_factory=dict)
    clients: list[dict[str, Any]] = field(default_factory=list)
    clients_by_leaf: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def apply_to(self, target: Any) -> None:
        """Copy the normalized topology state onto a runtime object."""
        target.topology_name = self.topology_name
        target.container_names = dict(self.container_names)
        target.topology_metadata = copy.deepcopy(self.topology_metadata)
        target.device_mgmt_ips = dict(self.device_mgmt_ips)
        target.clients = [copy.deepcopy(client) for client in self.clients]
        target.clients_by_leaf = {
            leaf: [copy.deepcopy(client) for client in clients] for leaf, clients in self.clients_by_leaf.items()
        }


def discover_topology_dir(base_dir: str) -> str:
    """Find the most likely generated topology directory for the current runtime."""
    env_dir = config.topology_dir
    if env_dir and os.path.exists(os.path.join(env_dir, "topology.json")):
        return env_dir

    generated_dirs = sorted(
        glob.glob(os.path.join(base_dir, "lab-topology", "generated_topology_*"))
        + glob.glob(os.path.join(base_dir, "lab-topology", "benchmarks", "generated_topology_*")),
        key=os.path.getmtime,
        reverse=True,
    )
    for candidate in generated_dirs:
        if os.path.exists(os.path.join(candidate, "topology.json")):
            return candidate

    return os.path.join(base_dir, "lab-topology")


def _extract_clients_by_leaf(clients: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for client in clients:
        leaf = client.get("leaf")
        if not leaf:
            continue
        grouped.setdefault(str(leaf), []).append(client)
    return grouped


def build_topology_state_from_metadata(metadata: dict[str, Any]) -> TopologyState:
    """Normalize generated topology metadata into a runtime state object."""
    metadata = metadata or {}
    topology_name = metadata.get("name", "dcn")
    devices = metadata.get("devices", {}) or {}

    container_names: dict[str, str] = {}
    device_mgmt_ips: dict[str, str] = {}
    clients: list[dict[str, Any]] = []

    for role in ("spines", "leafs", "clients"):
        for entry in devices.get(role, []) or []:
            name = entry.get("name")
            if not name:
                continue
            name = str(name)
            container_names[name] = clab_container_name(topology_name, name)
            mgmt_ip = entry.get("mgmt_ip")
            if mgmt_ip:
                device_mgmt_ips[name] = str(mgmt_ip)
            if role == "clients":
                clients.append(dict(entry))

    return TopologyState(
        topology_name=topology_name,
        container_names=container_names,
        topology_metadata=metadata,
        device_mgmt_ips=device_mgmt_ips,
        clients=clients,
        clients_by_leaf=_extract_clients_by_leaf(clients),
    )


def enrich_topology_metadata(topology: dict[str, Any], default_sonic_port_mtu: int = 9100) -> dict[str, Any]:
    """Add runtime semantics that help agents interpret generated topology metadata."""
    enriched = copy.deepcopy(topology or {})
    defaults = enriched.setdefault("defaults", {})
    link_mtu = defaults.get("link_mtu") or enriched.get("link_mtu")
    if isinstance(link_mtu, int) and link_mtu > 0:
        defaults["link_mtu"] = link_mtu
    defaults.setdefault("sonic_port_mtu", default_sonic_port_mtu)

    enriched.setdefault(
        "mtu_semantics",
        {
            "link_mtu_scope": "containerlab/client link MTU",
            "sonic_port_mtu_scope": "SONiC front-panel interface MTU",
            "note": (
                "Treat topology defaults.link_mtu as the client/container link budget. "
                "Healthy SONiC ports normally report MTU 9100 in sonic-vs, so 9232 vs 9100 "
                "alone is not evidence of mtu_mismatch."
            ),
        },
    )
    return enriched


def resolve_interface_metric_identities(interface: str) -> dict[str, list[str]]:
    """Return identity variants used across CLI, Linux, and InfluxDB tags."""
    safe_interface = str(interface or "").strip()
    if not safe_interface:
        return {"names": [], "paths": []}

    interface_names = {safe_interface}

    lower_name = safe_interface.lower()
    if lower_name.startswith("eth") and lower_name[3:].isdigit():
        eth_idx = int(lower_name[3:])
        if eth_idx >= 1:
            interface_names.add(f"Ethernet{(eth_idx - 1) * 4}")
    elif lower_name.startswith("ethernet") and lower_name[8:].isdigit():
        port_idx = int(lower_name[8:])
        interface_names.add(f"Ethernet{port_idx}")
        if port_idx % 4 == 0:
            interface_names.add(f"eth{(port_idx // 4) + 1}")

    sonic_names = sorted(name for name in interface_names if name.startswith("Ethernet"))
    paths: list[str] = []
    for name in sonic_names:
        paths.extend([f"COUNTERS/{name}", f"/COUNTERS/{name}"])
    return {
        "names": sorted(interface_names),
        "paths": paths,
    }


def _find_local_topology_dir(default_dir: str) -> str | None:
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    candidates = []
    for raw in [default_dir, "lab-topology"]:
        candidate = raw if os.path.isabs(raw) else os.path.join(repo_root, raw)
        candidates.append(candidate)
    candidates.extend(
        sorted(
            glob.glob(os.path.join(repo_root, "lab-topology", "generated_topology_*")),
            key=os.path.getmtime,
            reverse=True,
        )
    )
    seen = set()
    ordered = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
    for candidate in ordered:
        if not os.path.isdir(candidate):
            continue
        if os.path.exists(os.path.join(candidate, "dcn.clab.yaml")) and os.path.exists(
            os.path.join(candidate, "topology.json")
        ):
            return candidate
    for candidate in ordered:
        if not os.path.isdir(candidate):
            continue
        has_clab = bool(glob.glob(os.path.join(candidate, "*.clab.y*ml")))
        if has_clab and os.path.exists(os.path.join(candidate, "topology.json")):
            return candidate
    return None


def _is_worker_pool_topology_dir(path: str) -> bool:
    normalized = os.path.normpath(path)
    parts = normalized.split(os.sep)
    return "pools" in parts and "workers" in parts


def resolve_topology_dir(explicit_topology_dir: str | None, default_dir: str) -> str:
    if explicit_topology_dir:
        return explicit_topology_dir
    try:
        detect_cmd = [
            *sudo_prefix(),
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=containerlab",
            "--format",
            '{{.Label "clab-topo-file"}}',
        ]
        detect_result = safe_run(detect_cmd, capture_output=True, text=True, timeout=5)
        lines = [line.strip() for line in detect_result.stdout.splitlines() if line.strip()]
        detected_file = lines[0] if lines else ""
        if detected_file and os.path.exists(detected_file):
            topo_dir = os.path.dirname(detected_file)
            if not _is_worker_pool_topology_dir(topo_dir):
                return topo_dir
    except Exception:
        logger.debug("clab inspect-based topology detection failed", exc_info=True)
    local_topology_dir = _find_local_topology_dir(default_dir)
    if local_topology_dir:
        return local_topology_dir
    return default_dir


def resolve_topology_file(topo_dir: str) -> str | None:
    preferred = os.path.join(topo_dir, "dcn.clab.yaml")
    if os.path.exists(preferred):
        return preferred
    candidates = sorted(glob.glob(os.path.join(topo_dir, "*.clab.y*ml")))
    return candidates[0] if candidates else None


def infer_lab_name(topology_file: str) -> str | None:
    try:
        with open(topology_file, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                match = re.match(r"^name:\s*(\S+)", line)
                if match:
                    return match.group(1)
    except Exception:
        logger.debug("failed to read topology file %s for lab name", topology_file, exc_info=True)
    base = os.path.basename(topology_file)
    if base.endswith(".clab.yaml"):
        return base[: -len(".clab.yaml")]
    if base.endswith(".clab.yml"):
        return base[: -len(".clab.yml")]
    return None


def load_topology_metadata_file(topo_dir: str) -> dict | None:
    metadata_path = os.path.join(topo_dir, "topology.json")
    if not os.path.exists(metadata_path):
        return None
    try:
        with open(metadata_path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        logger.debug("failed to load topology metadata %s", metadata_path, exc_info=True)
        return None


def preview_topology_items(items: list[str], limit: int = 4) -> str:
    if not items:
        return "none"
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f", ... (+{len(items) - limit} more)"


def check_topology_deployed(topo_dir: str) -> tuple[bool, str]:
    topo_file = resolve_topology_file(topo_dir)
    if not topo_file:
        return False, f"No topology file found under '{topo_dir}'"
    lab_name = infer_lab_name(topo_file)
    if not lab_name:
        return False, f"Could not infer lab name from topology file: {topo_file}"
    try:
        result = safe_run(
            [*sudo_prefix(), "docker", "ps", "--filter", f"label=containerlab={lab_name}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception as e:
        return False, f"Failed to query docker runtime: {e}"
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return False, f"Docker query failed for lab '{lab_name}': {stderr or 'unknown error'}"
    running = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not running:
        return False, (
            f"No running containers found for lab '{lab_name}'. "
            "Deploy a lab with `bash scripts/runtime/deploy.sh <scale>` or use the SDK-managed runtime flow."
        )
    metadata = load_topology_metadata_file(topo_dir)
    if metadata:
        expected = []
        devices = metadata.get("devices", {}) or {}
        for group in ("spines", "leafs", "clients"):
            for device in devices.get(group, []) or []:
                name = str((device or {}).get("name") or "").strip()
                if name:
                    expected.append(clab_container_name(lab_name, name))
        expected_set = set(expected)
        running_set = set(running)
        if expected_set:
            missing = sorted(expected_set - running_set)
            unexpected = sorted(running_set - expected_set)
            if missing or unexpected:
                details = []
                if missing:
                    details.append(f"missing={preview_topology_items(missing)}")
                if unexpected:
                    details.append(f"unexpected={preview_topology_items(unexpected)}")
                return False, (
                    f"Running containers do not match topology metadata for lab '{lab_name}': " + "; ".join(details)
                )
    return True, f"Detected {len(running)} running container(s) for lab '{lab_name}'"
