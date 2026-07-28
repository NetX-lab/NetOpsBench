"""Placement coverage for the default large-topology scenario campaign."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

import pytest
import yaml

from netopsbench.platform.scenario import generator as scenario_generator
from netopsbench.platform.topology.generator import generate_topology

EXPANDED_FAULTS = {
    "bgp_neighbor_misconfig",
    "blackhole_route",
    "device_down",
    "high_latency",
    "link_down",
    "link_flapping",
    "mtu_mismatch",
    "packet_corruption",
    "packet_loss",
}
ACCESS_SCOPED_FAULTS = {
    "acl_misconfig",
    "route_policy_misconfig",
    "static_route_misconfig",
}


def _load_generated(paths: list[Path]) -> list[dict]:
    return [yaml.safe_load(path.read_text(encoding="utf-8")) for path in paths]


def _generate_default(scale: str, root: Path, *, reverse_templates: bool = False):
    topology_dir = root / f"topology-{scale}"
    if not topology_dir.exists():
        generate_topology(scale, str(topology_dir))
    topo = scenario_generator.load_topology(scale, str(topology_dir))
    spec = scenario_generator.load_yaml(scenario_generator.default_campaign_spec())
    if reverse_templates:
        spec["fault_templates"] = list(reversed(spec["fault_templates"]))
    paths = scenario_generator.generate(spec, topo, root / "scenarios", seed=42)
    return topo, _load_generated(paths)


def _role_counts(rows: list[dict], roles: dict[str, str], fault_type: str) -> Counter:
    return Counter(roles[row["episode"]["target_device"]] for row in rows if row["episode"]["fault_type"] == fault_type)


def test_large_campaign_uses_complete_pingmesh_window(tmp_path):
    _topology, rows = _generate_default("large", tmp_path)

    assert len(rows) == 52
    assert {row["episode"]["duration_seconds"] for row in rows} == {54}


@pytest.mark.parametrize("scale", ["xlarge", "fat-tree-k8", "fat-tree-k12"])
def test_default_large_campaign_has_balanced_tier_placements(tmp_path, scale):
    topo, rows = _generate_default(scale, tmp_path)
    fault_counts = Counter(row["episode"]["fault_type"] for row in rows)

    assert len(rows) == 70
    assert len({row["scenario_id"] for row in rows}) == 70
    assert all(fault_counts[fault] == 6 for fault in EXPANDED_FAULTS)
    assert all(fault_counts[fault] == 4 for fault in ACCESS_SCOPED_FAULTS)
    assert fault_counts["none"] == 4
    assert {
        row["episode"]["metadata"]["loss_pct"] for row in rows if row["episode"]["fault_type"] == "packet_loss"
    } == {30}

    indices_by_fault: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        indices_by_fault[row["episode"]["fault_type"]].append(int(row["scenario_id"].rsplit("_", 1)[1]))
    for fault_type, indices in indices_by_fault.items():
        assert sorted(indices) == list(range(1, fault_counts[fault_type] + 1))

    roles = {device.name: device.role.value for device in topo.manifest.devices}
    if scale == "xlarge":
        assert _role_counts(rows, roles, "link_down") == {"leaf": 4, "spine": 2}
        assert _role_counts(rows, roles, "device_down") == {"leaf": 3, "spine": 3}
        for fault in EXPANDED_FAULTS - {"device_down", "link_down"}:
            assert _role_counts(rows, roles, fault) == {"leaf": 3, "spine": 3}
    else:
        assert _role_counts(rows, roles, "link_down") == {"edge": 3, "agg": 2, "core": 1}
        for fault in {"device_down", "mtu_mismatch", "bgp_neighbor_misconfig", "blackhole_route"}:
            assert _role_counts(rows, roles, fault) == {"edge": 2, "agg": 2, "core": 2}
        for fault in {"high_latency", "packet_loss", "packet_corruption", "link_flapping"}:
            assert _role_counts(rows, roles, fault) == {"edge": 2, "agg": 3, "core": 1}

    by_template: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_template[row["metadata"]["generator"]["template"]].append(row)
    for template_rows in by_template.values():
        devices = [row["episode"].get("target_device") for row in template_rows]
        devices = [device for device in devices if device is not None]
        assert len(devices) == len(set(devices))

        pods = {
            int(next(device for device in topo.manifest.devices if device.name == name).metadata["pod"])
            for name in devices
            if roles[name] in {"edge", "agg"}
        }
        if len(devices) > 1 and all(roles[name] in {"edge", "agg"} for name in devices):
            assert len(pods) == len(devices)


def _normalize_interface(name: str) -> str:
    if name.startswith("eth") and name[3:].isdigit():
        return f"Ethernet{(int(name[3:]) - 1) * 4}"
    return name


def test_fat_tree_templates_select_the_requested_link_and_bgp_roles(tmp_path):
    topo, rows = _generate_default("fat-tree-k8", tmp_path)
    roles = {device.name: device.role.value for device in topo.manifest.devices}
    peers: dict[tuple[str, str], str] = {}
    for link in topo.manifest.links:
        left, right = link.endpoints
        peers[(left.device, _normalize_interface(left.interface))] = right.device
        peers[(right.device, _normalize_interface(right.interface))] = left.device

    expected_peer_role = {
        "link_down_access": "client",
        "link_down_leaf_fabric": "agg",
        "link_down_agg_edge": "edge",
        "link_down_agg_core": "core",
        "link_down_core_agg": "agg",
        "packet_loss_leaf": "agg",
        "packet_loss_spine": "agg",
        "packet_loss_agg_edge": "edge",
        "packet_loss_agg_core": "core",
    }
    for row in rows:
        template = row["metadata"]["generator"]["template"]
        if template not in expected_peer_role:
            continue
        episode = row["episode"]
        peer = peers[(episode["target_device"], episode["target_interface"])]
        assert roles[peer] == expected_peer_role[template]

    bgp_by_template = {
        row["metadata"]["generator"]["template"]: row
        for row in rows
        if row["episode"]["fault_type"] == "bgp_neighbor_misconfig"
    }
    for template, expected_role in (
        ("bgp_neighbor_misconfig_agg_edge", "downlink"),
        ("bgp_neighbor_misconfig_agg_core", "uplink"),
    ):
        episode = bgp_by_template[template]["episode"]
        peer_ip = episode["metadata"]["peer_ip"]
        neighbor = next(item for item in topo.bgp_neighbors[episode["target_device"]] if item["peer_ip"] == peer_ip)
        assert neighbor["interface_role"] == expected_role


def test_xlarge_link_down_templates_select_the_requested_peer_roles(tmp_path):
    topo, rows = _generate_default("xlarge", tmp_path)
    roles = {device.name: device.role.value for device in topo.manifest.devices}
    peers: dict[tuple[str, str], str] = {}
    for link in topo.manifest.links:
        left, right = link.endpoints
        peers[(left.device, _normalize_interface(left.interface))] = right.device
        peers[(right.device, _normalize_interface(right.interface))] = left.device

    expected_peer_role = {
        "link_down_access": "client",
        "link_down_leaf_fabric": "spine",
        "link_down_spine_fabric": "leaf",
        "packet_loss_leaf": "spine",
        "packet_loss_spine": "leaf",
    }
    for row in rows:
        template = row["metadata"]["generator"]["template"]
        if template not in expected_peer_role:
            continue
        episode = row["episode"]
        peer = peers[(episode["target_device"], episode["target_interface"])]
        assert roles[peer] == expected_peer_role[template]


def test_template_rng_is_stable_when_campaign_order_changes(tmp_path):
    topology_root = tmp_path / "shared"
    topo, forward = _generate_default("fat-tree-k8", topology_root)
    del topo
    _, reverse = _generate_default("fat-tree-k8", topology_root / "reverse", reverse_templates=True)

    def signatures(rows):
        grouped = defaultdict(list)
        for row in rows:
            episode = row["episode"]
            grouped[row["metadata"]["generator"]["template"]].append(
                (
                    episode.get("target_device"),
                    episode.get("target_interface"),
                    episode.get("target_prefix"),
                    episode.get("metadata", {}).get("peer_ip"),
                    episode.get("metadata", {}).get("target_ip"),
                )
            )
        return {name: sorted(values, key=str) for name, values in grouped.items()}

    assert signatures(forward) == signatures(reverse)


def test_link_down_without_interface_role_defaults_to_access_link(tmp_path):
    topology_dir = tmp_path / "topology"
    generate_topology("small", str(topology_dir))
    topo = scenario_generator.load_topology("small", str(topology_dir))
    spec = {
        "defaults": {"count_per_fault": 1},
        "fault_templates": [{"name": "default_access_link_down", "fault_type": "link_down", "device_role": "leaf"}],
    }
    rows = _load_generated(scenario_generator.generate(spec, topo, tmp_path / "scenarios", seed=7))
    episode = rows[0]["episode"]

    link = next(
        link
        for link in topo.manifest.links
        if any(
            endpoint.device == episode["target_device"]
            and _normalize_interface(endpoint.interface) == episode["target_interface"]
            for endpoint in link.endpoints
        )
    )
    peer = next(endpoint.device for endpoint in link.endpoints if endpoint.device != episode["target_device"])
    roles = {device.name: device.role.value for device in topo.manifest.devices}
    assert roles[peer] == "client"


def test_static_route_template_remains_access_scoped(tmp_path):
    topology_dir = tmp_path / "topology"
    generate_topology("fat-tree-k8", str(topology_dir))
    topo = scenario_generator.load_topology("fat-tree-k8", str(topology_dir))
    spec = {
        "defaults": {"count_per_fault": 1},
        "fault_templates": [
            {
                "name": "static_route_must_remain_at_origin",
                "fault_type": "static_route_misconfig",
                "device_role": "agg",
            }
        ],
    }
    rows = _load_generated(scenario_generator.generate(spec, topo, tmp_path / "scenarios", seed=7))
    target = rows[0]["episode"]["target_device"]
    roles = {device.name: device.role.value for device in topo.manifest.devices}

    assert roles[target] == "edge"
