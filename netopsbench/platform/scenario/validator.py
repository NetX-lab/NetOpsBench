"""Scenario validation: schema constraints and topology compatibility."""

from __future__ import annotations

import json
import os
import re

from netopsbench.platform.faults.specs import (
    canonicalize_fault_name,
    get_fault_spec,
    get_supported_scenario_faults,
)
from netopsbench.platform.utils.interface_names import interface_aliases

from .models import Scenario
from .parser import episode_from_dict, episode_to_dict

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SUPPORTED_TOPOLOGY_SCALES = ["xs", "small", "medium", "large"]
_SUPPORTED_TRAFFIC_PROFILES = ["light", "standard", "stress"]

CLIENT_COUNT_TO_SCALE = {
    2: "xs",
    4: "xs",
    8: "small",
    16: "medium",
    32: "large",
    64: "large",
}

NETWORK_DEVICE_PREFIXES = ("spine", "leaf")

# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def supported_scenario_faults() -> list[str]:
    faults = sorted(set(get_supported_scenario_faults()))
    return ["none", *faults]


def validate_scenario(scenario: Scenario) -> list[str]:
    """Validate a scenario's schema and fault-specific constraints."""
    errors: list[str] = []

    if not scenario.scenario_id:
        errors.append("Missing scenario_id")
    if not scenario.name:
        errors.append("Missing scenario name")
    if not scenario.episodes:
        errors.append("No episodes defined")
    if scenario.topology_scale not in _SUPPORTED_TOPOLOGY_SCALES:
        errors.append(f"Invalid topology_scale: {scenario.topology_scale}")
    if scenario.traffic_profile not in _SUPPORTED_TRAFFIC_PROFILES:
        errors.append(f"Invalid traffic_profile: {scenario.traffic_profile}")

    canonical_fault_types = [canonicalize_fault_name(ep.fault_type) for ep in scenario.episodes]
    has_fault_episode = any(fault_type != "none" for fault_type in canonical_fault_types)
    if has_fault_episode:
        difficulty = (scenario.metadata or {}).get("difficulty")
        if difficulty not in ["easy", "medium", "hard"]:
            errors.append("Scenario metadata requires difficulty in [easy, medium, hard] for benchmark scoring")

        expected_diagnosis = canonicalize_fault_name((scenario.metadata or {}).get("expected_diagnosis"))
        if not expected_diagnosis:
            errors.append("Scenario metadata missing expected_diagnosis for benchmark scoring")

    supported_faults = supported_scenario_faults()
    for i, episode in enumerate(scenario.episodes):
        canonical_fault_type = canonicalize_fault_name(episode.fault_type)
        if not episode.episode_id:
            errors.append(f"Episode {i}: Missing episode_id")
        if not canonical_fault_type:
            errors.append(f"Episode {i}: Missing fault_type")
        if canonical_fault_type not in supported_faults:
            errors.append(
                f"Episode {i}: Unsupported fault_type '{canonical_fault_type}'. "
                f"Supported values: {supported_faults}"
            )
        if canonical_fault_type != "none" and not episode.target_device:
            errors.append(f"Episode {i}: Missing target_device")

        spec = get_fault_spec(canonical_fault_type)
        if spec is not None:
            episode_view = episode_from_dict({**episode_to_dict(episode), "fault_type": canonical_fault_type})
            errors.extend(spec.validate_episode(episode_view, episode_index=i))

    return errors


# ---------------------------------------------------------------------------
# Topology validation
# ---------------------------------------------------------------------------


def load_topology_metadata(topology_dir: str) -> dict:
    """Load topology metadata from <topology_dir>/topology.json."""
    metadata_path = os.path.join(topology_dir, "topology.json")
    if not os.path.exists(metadata_path):
        raise FileNotFoundError(
            f"Topology metadata not found: {metadata_path}. "
            "Run deploy/generation first or provide --topology-dir with topology.json."
        )

    with open(metadata_path, encoding="utf-8") as handle:
        return json.load(handle)


def infer_topology_scale(metadata: dict) -> str:
    """Infer topology scale from metadata explicit fields or client count."""
    if not isinstance(metadata, dict):
        return "unknown"

    explicit = metadata.get("topology_scale") or metadata.get("scale_name")
    if isinstance(explicit, str):
        return explicit

    scale_block = metadata.get("scale", {})
    if isinstance(scale_block, dict):
        explicit_from_scale = scale_block.get("name")
        if isinstance(explicit_from_scale, str):
            return explicit_from_scale

        total_clients = scale_block.get("total_clients")
        if isinstance(total_clients, int):
            return CLIENT_COUNT_TO_SCALE.get(total_clients, "unknown")

    clients = metadata.get("devices", {}).get("clients", [])
    if isinstance(clients, list):
        return CLIENT_COUNT_TO_SCALE.get(len(clients), "unknown")

    return "unknown"


def _all_topology_device_names(metadata: dict) -> set[str]:
    names: set[str] = set()
    devices = metadata.get("devices", {}) if isinstance(metadata, dict) else {}
    for role in ("spines", "leafs", "clients"):
        for device in devices.get(role, []) or []:
            name = device.get("name")
            if name:
                names.add(name)
    return names


def _parse_config_interfaces(config_path: str) -> set[str]:
    interfaces: set[str] = set()
    if not os.path.exists(config_path):
        return interfaces

    try:
        with open(config_path, encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if line.startswith("config interface startup"):
                    parts = line.split()
                    if len(parts) >= 4:
                        interfaces.add(parts[3])
                elif line.startswith("config interface ip add"):
                    parts = line.split()
                    if len(parts) >= 5:
                        interfaces.add(parts[4])
    except (OSError, ValueError):
        return set()

    return interfaces


def _validate_episode_target_interface(
    topology_dir: str,
    target_device: str,
    target_interface: str,
    scenario_id: str,
    episode_id: str,
) -> list[str]:
    if not target_interface:
        return []
    if not target_device.startswith(NETWORK_DEVICE_PREFIXES):
        return []

    config_path = os.path.join(topology_dir, "configs", f"{target_device}.sh")
    config_interfaces = _parse_config_interfaces(config_path)

    if not config_interfaces:
        if re.match(r"^(Ethernet\d+|eth\d+|e\d+-\d+|ethernet-\d+/\d+)$", target_interface):
            return []
        return [
            (
                f"[scenario={scenario_id} episode={episode_id}] Invalid target_interface "
                f"'{target_interface}' for device '{target_device}'. "
                "Expected format EthernetX or ethX for network devices."
            )
        ]

    allowed_aliases: set[str] = set()
    for iface in config_interfaces:
        allowed_aliases.update(interface_aliases(iface))

    if target_interface in allowed_aliases:
        return []

    return [
        (
            f"[scenario={scenario_id} episode={episode_id}] target_interface '{target_interface}' "
            f"not found on device '{target_device}' in topology config. "
            f"Examples: {sorted(list(allowed_aliases))[:6]}"
        )
    ]


def validate_scenario_topology(scenario, topology_dir: str) -> dict:
    """Validate scenario topology compatibility and episode target consistency."""
    metadata = load_topology_metadata(topology_dir)
    actual_scale = infer_topology_scale(metadata)
    declared_scale = scenario.topology_scale

    errors: list[str] = []
    warnings: list[str] = []

    if actual_scale == "unknown":
        errors.append(
            f"[scenario={scenario.scenario_id}] Could not infer actual topology scale from {topology_dir}/topology.json"
        )
    elif declared_scale != actual_scale:
        errors.append(
            f"[scenario={scenario.scenario_id}] topology_scale mismatch: declared='{declared_scale}' "
            f"actual='{actual_scale}'. Use matching scenario files or redeploy the topology."
        )

    device_names = _all_topology_device_names(metadata)
    for episode in scenario.episodes:
        if episode.fault_type != "none" and episode.target_device not in device_names:
            errors.append(
                f"[scenario={scenario.scenario_id} episode={episode.episode_id}] target_device "
                f"'{episode.target_device}' not found in topology devices {sorted(device_names)}"
            )
        if episode.target_interface and episode.target_device:
            errors.extend(
                _validate_episode_target_interface(
                    topology_dir=topology_dir,
                    target_device=episode.target_device,
                    target_interface=episode.target_interface,
                    scenario_id=scenario.scenario_id,
                    episode_id=episode.episode_id,
                )
            )

    status = "fail" if errors else "pass"
    return {
        "status": status,
        "declared_scale": declared_scale,
        "actual_scale": actual_scale,
        "errors": errors,
        "warnings": warnings,
        "topology_devices": sorted(device_names),
    }


__all__ = [
    "supported_scenario_faults",
    "validate_scenario",
    "load_topology_metadata",
    "infer_topology_scale",
    "validate_scenario_topology",
]
