"""Ground-truth construction shared by benchmark and simulator execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from netopsbench.models.topology import DeviceRole
from netopsbench.platform.topology.topology_utils import load_topology_manifest
from netopsbench.platform.utils.interface_names import are_interfaces_equivalent, to_sonic_interface

_INTERFACE_SYMMETRIC_FAULT_TYPES = {
    "link_down",
    "link_flapping",
    "packet_loss",
    "packet_corruption",
    "high_latency",
    "mtu_mismatch",
}


def _find_link_peer_locations(
    topology_dir: str | None,
    target_device: str | None,
    target_interface: str | None,
) -> list[dict[str, str]]:
    if not topology_dir or not target_device or not target_interface:
        return []
    manifest = load_topology_manifest(Path(topology_dir))

    def location(device: str, interface: str) -> dict[str, str]:
        peer = manifest.device(device)
        if peer is not None and peer.role is not DeviceRole.CLIENT:
            interface = to_sonic_interface(interface)
        return {"device": device, "interface": interface}

    for link in manifest.links:
        left, right = link.endpoints
        if left.device == target_device and are_interfaces_equivalent(left.interface, target_interface):
            return [location(right.device, right.interface)]
        if right.device == target_device and are_interfaces_equivalent(right.interface, target_interface):
            return [location(left.device, left.interface)]
    return []


def build_episode_ground_truth(
    episode_info: dict[str, Any],
    topology_dir: str | None = None,
) -> dict[str, Any]:
    location = {"device": episode_info.get("target_device")}
    if episode_info.get("target_interface"):
        location["interface"] = episode_info.get("target_interface")
    ground_truth = {"fault_type": episode_info.get("fault_type"), "location": location}
    if episode_info.get("fault_type") in _INTERFACE_SYMMETRIC_FAULT_TYPES:
        equivalent_locations = _find_link_peer_locations(
            topology_dir=topology_dir,
            target_device=episode_info.get("target_device"),
            target_interface=episode_info.get("target_interface"),
        )
        if equivalent_locations:
            ground_truth["equivalent_locations"] = equivalent_locations
    return ground_truth


__all__ = ["build_episode_ground_truth"]
