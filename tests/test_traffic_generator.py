from collections import Counter, defaultdict

import pytest

from netopsbench.models import topology as topology_models
from netopsbench.models.profiles import get_scale_profile
from netopsbench.models.topology import Collector, Device, DeviceRole, Management, TopologyManifest
from netopsbench.platform.topology.generator import generate_topology
from netopsbench.platform.topology.topology_utils import load_topology_manifest
from netopsbench.platform.traffic import generator as traffic_generator
from netopsbench.platform.traffic.settings import DEFAULT_SWITCH_PPS_LIMIT, TrafficSettings


def _canonical_traffic_topology(
    *,
    scale: str,
    family: str = "clos",
    spines: int = 2,
    leafs: int = 1,
    clients_per_leaf: int = 2,
) -> dict:
    devices = [
        *[Device(name=f"spine{i}", role=DeviceRole.SPINE) for i in range(1, spines + 1)],
        *[Device(name=f"leaf{i}", role=DeviceRole.LEAF) for i in range(1, leafs + 1)],
    ]
    for idx in range(1, (leafs * clients_per_leaf) + 1):
        leaf_idx = ((idx - 1) // clients_per_leaf) + 1
        devices.append(
            Device(
                name=f"client{idx}",
                role=DeviceRole.CLIENT,
                data_ip=f"192.168.{100 + leaf_idx}.{(((idx - 1) % clients_per_leaf) * 4) + 2}",
                attached_switch=f"leaf{leaf_idx}",
                metadata={"rack": f"rack{leaf_idx}"},
            )
        )
    manifest = TopologyManifest(
        topology_id=scale,
        name=scale,
        scale=scale,
        family=family,
        management=Management(network=f"clab-{scale}", ipv4_subnet="172.20.20.0/24"),
        collector=Collector(ipv4="172.20.20.200"),
        defaults=topology_models.TopologyDefaults(),
        facts=topology_models.TopologyFacts(
            num_spines=spines,
            num_leafs=leafs,
            clients_per_attached_switch=clients_per_leaf,
            total_clients=leafs * clients_per_leaf,
            total_switches=spines + leafs,
        ),
        routing=topology_models.RoutingMetadata(
            ecmp_hash_policy_by_role={device.role: 1 for device in devices if device.role is not DeviceRole.CLIENT}
        ),
        devices=devices,
        links=[],
    )
    return manifest.model_dump(mode="json")


def test_intra_leaf_budget_counts_both_ingress_and_egress():
    topology = _canonical_traffic_topology(scale="xs", spines=0, leafs=1, clients_per_leaf=2)
    settings = TrafficSettings(switch_pps_limit=300)

    config = traffic_generator.generate_traffic_config_from_topology(topology, "xs", "standard", settings=settings)

    flow_pps = sum(traffic_generator.estimate_flow_pps(flow) for flow in config["flows"])
    assert config["stats"]["total_flows"] == 2
    assert config["stats"]["estimated_switch_pps"]["max_leaf_pps"] == pytest.approx(flow_pps * 2)
    assert traffic_generator.validate_traffic_config(config, "xs", settings=settings) is True


def test_large_standard_profile_respects_switch_budget(tmp_path):
    result = generate_topology("large", str(tmp_path))

    config = traffic_generator.generate_traffic_config_from_topology(
        load_topology_manifest(result["metadata_file"]).model_dump(mode="json"),
        "large",
        "standard",
    )

    assert config["stats"]["total_flows"] > 0
    assert config["stats"]["estimated_switch_pps"]["max_leaf_pps"] <= DEFAULT_SWITCH_PPS_LIMIT
    assert config["stats"]["estimated_switch_pps"]["max_spine_pps"] <= DEFAULT_SWITCH_PPS_LIMIT
    assert traffic_generator.validate_traffic_config(config, "large") is True


def test_xlarge_standard_profile_respects_switch_budget(tmp_path):
    result = generate_topology("xlarge", str(tmp_path))

    config = traffic_generator.generate_traffic_config_from_topology(
        load_topology_manifest(result["metadata_file"]).model_dump(mode="json"),
        "xlarge",
        "standard",
    )

    assert config["stats"]["total_flows"] > 0
    assert config["stats"]["estimated_switch_pps"]["max_leaf_pps"] <= DEFAULT_SWITCH_PPS_LIMIT
    assert config["stats"]["estimated_switch_pps"]["max_spine_pps"] <= DEFAULT_SWITCH_PPS_LIMIT
    assert traffic_generator.validate_traffic_config(config, "xlarge") is True


def test_fat_tree_profiles_are_bounded(tmp_path):
    from netopsbench.platform.topology.generator import generate_topology

    k8_result = generate_topology("fat-tree-k8", str(tmp_path / "k8"))
    k12_result = generate_topology("fat-tree-k12", str(tmp_path / "k12"))
    k8_metadata = load_topology_manifest(k8_result["metadata_file"]).model_dump(mode="json")
    k12_metadata = load_topology_manifest(k12_result["metadata_file"]).model_dump(mode="json")

    k8_config = traffic_generator.generate_traffic_config_from_topology(k8_metadata, "fat-tree-k8", "standard")
    k12_config = traffic_generator.generate_traffic_config_from_topology(k12_metadata, "fat-tree-k12", "standard")

    assert k8_config["stats"]["total_flows"] > 0
    assert k12_config["stats"]["total_flows"] > 0
    for config in (k8_config, k12_config):
        switch_pps = config["stats"]["estimated_switch_pps"]
        assert switch_pps["edges"]
        assert switch_pps["aggs"]
        assert switch_pps["cores"]
        assert switch_pps["max_edge_pps"] > 0
        assert switch_pps["max_agg_pps"] > 0
        assert switch_pps["max_core_pps"] > 0
        assert switch_pps["max_switch_pps"] <= DEFAULT_SWITCH_PPS_LIMIT
        assert switch_pps["leafs"] == {}
        assert switch_pps["spines"] == {}
    assert (
        get_scale_profile("fat-tree-k12").traffic_max_pps_per_client
        < get_scale_profile("fat-tree-k8").traffic_max_pps_per_client
    )
    assert traffic_generator.validate_traffic_config(k8_config, "fat-tree-k8") is True
    assert traffic_generator.validate_traffic_config(k12_config, "fat-tree-k12") is True


@pytest.mark.parametrize(
    ("scale", "expected_flows", "expected_incoming"),
    [
        ("xs", 2, 1),
        ("small", 32, 4),
        ("medium", 64, 4),
        ("xlarge", 512, 4),
        ("fat-tree-k8", 512, 4),
        ("fat-tree-k12", 576, 4),
    ],
)
def test_standard_matrix_balances_destination_listener_budget(tmp_path, scale, expected_flows, expected_incoming):
    from netopsbench.platform.topology.generator import generate_topology

    result = generate_topology(scale, str(tmp_path / scale))
    topology = load_topology_manifest(result["metadata_file"]).model_dump(mode="json")

    config = traffic_generator.generate_traffic_config_from_topology(topology, scale, "standard")

    incoming = Counter(flow["dst"] for flow in config["flows"])
    listener_ports: dict[str, set[int]] = defaultdict(set)
    source_protocols: dict[str, Counter] = defaultdict(Counter)
    for flow in config["flows"]:
        listener_ports[flow["dst"]].add(flow["dst_port"])
        source_protocols[flow["src"]][flow["protocol"]] += 1

    assert len(config["flows"]) == expected_flows
    assert set(incoming.values()) == {expected_incoming}
    assert all(ports == set(range(5201, 5201 + expected_incoming)) for ports in listener_ports.values())
    if scale != "xs":
        assert all(protocols == {"udp": 2, "tcp": 2} for protocols in source_protocols.values())
    assert config["stats"]["incoming_flows_per_client"] == dict(sorted(incoming.items()))
    assert config["stats"]["min_incoming_flows"] == expected_incoming
    assert config["stats"]["max_incoming_flows"] == expected_incoming
    assert config["stats"]["required_listener_ports"] == list(range(5201, 5201 + expected_incoming))


def test_large_standard_matrix_never_exceeds_four_destination_listeners(tmp_path):
    from netopsbench.platform.topology.generator import generate_topology

    result = generate_topology("large", str(tmp_path / "large"))
    topology = load_topology_manifest(result["metadata_file"]).model_dump(mode="json")

    config = traffic_generator.generate_traffic_config_from_topology(topology, "large", "standard")
    incoming = Counter(flow["dst"] for flow in config["flows"])

    assert config["stats"]["total_flows"] > 0
    assert max(incoming.values()) <= 4
    assert set(flow["dst_port"] for flow in config["flows"]) <= {5201, 5202, 5203, 5204}


@pytest.mark.parametrize("profile", ["light", "stress"])
def test_traffic_generator_rejects_nonstandard_profiles(profile):
    topology = _canonical_traffic_topology(scale="xs")
    with pytest.raises(ValueError, match="Only the standard traffic profile is supported"):
        traffic_generator.generate_traffic_config_from_topology(topology, "xs", profile)


def test_standard_matrix_is_deterministic(tmp_path):
    from netopsbench.platform.topology.generator import generate_topology

    result = generate_topology("fat-tree-k8", str(tmp_path / "k8"))
    topology = load_topology_manifest(result["metadata_file"]).model_dump(mode="json")

    first = traffic_generator.generate_traffic_config_from_topology(topology, "fat-tree-k8", "standard")
    second = traffic_generator.generate_traffic_config_from_topology(topology, "fat-tree-k8", "standard")

    assert first == second


def test_traffic_generator_rejects_legacy_grouped_topology():
    with pytest.raises(ValueError, match="schema_version.*3"):
        traffic_generator.generate_traffic_config_from_topology(
            {"devices": {"clients": [], "leafs": [], "spines": []}},
            "xs",
            "standard",
        )
