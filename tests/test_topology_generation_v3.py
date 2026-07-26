"""Focused contracts for unified topology planning and schema-v3 rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from netopsbench.models.topology import DeviceRole, TopologyManifest
from netopsbench.platform.topology.clos_builder import build_clos_plan
from netopsbench.platform.topology.config import TopologyConfig, config_for_scale
from netopsbench.platform.topology.generator import generate_topology
from netopsbench.platform.topology.plan import FabricPlan
from netopsbench.platform.topology.topology_utils import (
    clab_container_name,
    coerce_topology_manifest,
    load_topology_manifest,
)


@pytest.mark.parametrize(
    ("scale", "roles", "clients", "links"),
    [
        ("xs", {"spine": 2, "leaf": 2}, 2, 6),
        ("xlarge", {"spine": 16, "leaf": 128}, 128, 2176),
        ("fat-tree-k8", {"core": 16, "agg": 32, "edge": 32}, 128, 384),
        ("fat-tree-k12", {"core": 36, "agg": 72, "edge": 72}, 144, 1008),
    ],
)
def test_renderer_persists_only_canonical_schema_v3_devices(tmp_path, scale, roles, clients, links):
    result = generate_topology(scale, str(tmp_path / scale))
    persisted = json.loads(Path(result["metadata_file"]).read_text(encoding="utf-8"))
    manifest = TopologyManifest.model_validate(persisted)

    assert persisted["schema_version"] == "3"
    assert isinstance(persisted["devices"], list)
    assert len(manifest.links) == links
    assert all(link.mtu == 9232 for link in manifest.links)
    assert all(endpoint.interface for link in manifest.links for endpoint in link.endpoints)
    assert len(manifest.clients()) == clients
    assert all(client.attached_switch for client in manifest.clients())
    assert result["metadata"] == manifest.model_dump(mode="json")
    assert result["agent_topology"] == manifest.to_agent_topology()

    for role, count in roles.items():
        assert len(manifest.devices_by_role(role)) == count
    for client in (entry for entry in persisted["devices"] if entry["role"] == "client"):
        assert "leaf" not in client
        assert "edge" not in client


def test_renderer_atomically_removes_stale_device_artifacts(tmp_path):
    output = tmp_path / "topology"
    generate_topology("small", str(output))
    assert (output / "configs" / "sonic" / "leaf4").is_dir()

    generate_topology("xs", str(output))

    assert not (output / "configs" / "sonic" / "leaf4").exists()
    assert not (output / "configs" / "frr" / "leaf4.conf").exists()
    assert not list(tmp_path.glob(".topology.staging-*"))
    assert not (tmp_path / ".topology.backup").exists()


def test_clos_builder_returns_complete_fabric_plan_without_writing(tmp_path):
    output_dir = tmp_path / "not-rendered"
    plan = build_clos_plan(TopologyConfig(scale_name="xs"))

    assert isinstance(plan, FabricPlan)
    assert not output_dir.exists()
    assert len(plan.device_plans) == 6
    spine1 = next(device for device in plan.device_plans if device.name == "spine1")
    leaf1 = next(device for device in plan.device_plans if device.name == "leaf1")
    assert spine1.required_ports == 2
    assert spine1.configdb_interface_cidrs["Ethernet0"] == ("10.1.1.1/30",)
    assert spine1.bgp_neighbors[0].peer_ip == "10.1.1.2"
    assert leaf1.required_ports == 3
    assert leaf1.bgp_networks == ("192.168.101.0/30",)
    assert plan.manifest.links[0].endpoints[0].interface == "eth1"


def test_rendered_clos_artifacts_keep_preseed_and_addressing_contract(tmp_path):
    result = generate_topology("xlarge", str(tmp_path))
    manifest = TopologyManifest.model_validate(result["metadata"])
    rendered = yaml.safe_load(Path(result["yaml_file"]).read_text(encoding="utf-8"))
    nodes = rendered["topology"]["nodes"]
    links = rendered["topology"]["links"]

    assert len(nodes) == 16 + 128 + 128
    assert len(links) == (16 * 128) + 128
    assert manifest.routing.ecmp_hash_policy_by_role == {DeviceRole.SPINE: 1, DeviceRole.LEAF: 1}
    assert nodes["spine16"]["mgmt-ipv4"] == "172.20.20.26"
    assert nodes["leaf128"]["mgmt-ipv4"] == "172.20.20.154"
    assert nodes["client128"]["mgmt-ipv4"] == "172.20.21.26"
    assert links[0] == {"endpoints": ["spine1:eth1", "leaf1:eth1"], "mtu": 9232}
    assert links[2047] == {"endpoints": ["spine16:eth128", "leaf128:eth16"], "mtu": 9232}
    assert links[-1] == {"endpoints": ["leaf128:eth17", "client128:eth1"], "mtu": 9232}

    assert (tmp_path / "configs" / "sonic" / "start.sh").is_file()
    assert (tmp_path / "configs" / "client-agent" / "client-agent.json").is_file()
    spine = json.loads((tmp_path / "configs" / "sonic" / "spine16" / "config_db.json").read_text())
    leaf = json.loads((tmp_path / "configs" / "sonic" / "leaf128" / "config_db.json").read_text())
    assert len([name for name in spine["PORT"]]) == 128
    assert len([name for name in leaf["PORT"]]) == 17
    assert "Ethernet508|10.16.128.1/30" in spine["INTERFACE"]
    assert "Ethernet64|192.168.228.1/30" in leaf["INTERFACE"]


@pytest.mark.parametrize(
    ("scale", "first_core", "first_agg", "first_edge", "switch_links"),
    [
        ("fat-tree-k8", 8, 8, 8, 256),
        ("fat-tree-k12", 12, 12, 8, 864),
    ],
)
def test_rendered_fat_tree_artifacts_keep_ports_and_disjoint_switch_pools(
    tmp_path, scale, first_core, first_agg, first_edge, switch_links
):
    result = generate_topology(scale, str(tmp_path / scale))
    root = Path(result["metadata_file"]).parent

    def config(device: str) -> dict:
        return json.loads((root / "configs" / "sonic" / device / "config_db.json").read_text())

    core = config("core1")
    agg = config("agg1")
    edge = config("edge1")
    assert len([key for key in core["INTERFACE"] if "|" not in key]) == first_core
    assert len([key for key in agg["INTERFACE"] if "|" not in key]) == first_agg
    assert len([key for key in edge["INTERFACE"] if "|" not in key]) == first_edge

    manifest = load_topology_manifest(root)
    switch_fabric_links = [link for link in manifest.links if link.kind in {"core-agg", "agg-edge"}]
    assert len(switch_fabric_links) == switch_links

    core_agg_networks = {
        cidr.split("|", 1)[1]
        for device in manifest.devices_by_role(DeviceRole.CORE)
        for cidr in config(device.name)["INTERFACE"]
        if "|" in cidr
    }
    agg_edge_networks = {
        cidr.split("|", 1)[1]
        for device in manifest.devices_by_role(DeviceRole.EDGE)
        for cidr in config(device.name)["INTERFACE"]
        if "|" in cidr and cidr.split("|", 1)[1].startswith("10.")
    }
    assert all(cidr.startswith("10.1.") for cidr in core_agg_networks)
    assert all(cidr.startswith("10.2.") for cidr in agg_edge_networks)


def test_topology_helpers_require_v3_and_manifest_owns_role_queries(tmp_path):
    generate_topology("fat-tree-k8", str(tmp_path))
    manifest = load_topology_manifest(tmp_path)
    persisted = json.loads((tmp_path / "topology.json").read_text(encoding="utf-8"))

    assert persisted == manifest.model_dump(mode="json")
    assert len(manifest.switches()) == 80
    expected = [clab_container_name(manifest.name, device.name) for device in manifest.devices]
    assert "clab-dcn-agg1" in expected
    assert len(expected) == 208
    assert manifest.clients()[0].attached_switch == "edge1"

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "topology.json").write_text(json.dumps({"name": "legacy", "devices": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version.*3"):
        load_topology_manifest(legacy)
    with pytest.raises(ValueError, match="schema_version.*3"):
        coerce_topology_manifest({"name": "legacy", "devices": {}})


def test_scale_config_factory_returns_fresh_values(tmp_path):
    original = config_for_scale("xs")
    separate = config_for_scale("xs")

    result = generate_topology("xs", str(tmp_path), name="custom-lab")

    assert result["metadata"]["name"] == "custom-lab"
    assert original is not separate
    assert original.name == separate.name == "dcn"
