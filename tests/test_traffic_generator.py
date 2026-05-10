import copy

from netopsbench.platform.topology.generator import TOPOLOGY_SCALES, TopologyGenerator
from netopsbench.platform.traffic import generator as traffic_generator


def test_intra_leaf_budget_counts_both_ingress_and_egress(monkeypatch):
    monkeypatch.setattr(traffic_generator, "SWITCH_PPS_LIMIT", 300)

    topology = {
        "devices": {
            "leafs": [{"name": "leaf1"}],
            "spines": [],
            "clients": [
                {"name": "client1", "data_ip": "192.168.1.2", "leaf": "leaf1"},
                {"name": "client2", "data_ip": "192.168.1.6", "leaf": "leaf1"},
            ],
        }
    }

    config = traffic_generator.generate_traffic_config_from_topology(topology, "xs", "standard")

    assert config["stats"]["total_flows"] == 0
    assert config["stats"]["estimated_switch_pps"]["max_leaf_pps"] == 0.0
    assert traffic_generator.validate_traffic_config(config, "xs") is True


def test_large_standard_profile_respects_switch_budget(tmp_path):
    topo_config = copy.deepcopy(TOPOLOGY_SCALES["large"])
    result = TopologyGenerator(config=topo_config, output_dir=str(tmp_path)).generate()

    config = traffic_generator.generate_traffic_config_from_topology(
        result["metadata"],
        "large",
        "standard",
    )

    assert config["stats"]["total_flows"] > 0
    assert config["stats"]["estimated_switch_pps"]["max_leaf_pps"] <= traffic_generator.SWITCH_PPS_LIMIT
    assert config["stats"]["estimated_switch_pps"]["max_spine_pps"] <= traffic_generator.SWITCH_PPS_LIMIT
    assert traffic_generator.validate_traffic_config(config, "large") is True
