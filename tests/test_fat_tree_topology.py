import ipaddress
import json
from pathlib import Path

from netopsbench.models.topology import DeviceRole, TopologyManifest
from netopsbench.platform.topology.configdb_payload import interface_networks_for_config
from netopsbench.platform.topology.generator import generate_topology


def _load_config(topology_dir: Path, device: str) -> dict:
    return json.loads((topology_dir / "configs" / "sonic" / device / "config_db.json").read_text(encoding="utf-8"))


def _assert_preseed_artifacts(topology_dir: Path, device: str) -> None:
    sonic_dir = topology_dir / "configs" / "sonic" / device
    assert (sonic_dir / "config_db.json").exists()
    assert (sonic_dir / "port_config.ini").exists()
    assert (sonic_dir / "lanemap.ini").exists()
    frr_config = topology_dir / "configs" / "frr" / f"{device}.conf"
    assert frr_config.exists()
    rendered = frr_config.read_text(encoding="utf-8")
    assert "bgp bestpath as-path multipath-relax" in rendered
    assert "maximum-paths 64" in rendered


def test_fat_tree_k12_sparse_generates_valid_non_overlapping_link_networks(tmp_path):
    result = generate_topology("fat-tree-k12", str(tmp_path))
    metadata = result["agent_topology"]
    manifest = TopologyManifest.model_validate_json(Path(result["metadata_file"]).read_text(encoding="utf-8"))

    assert metadata["topology_scale"] == "fat-tree-k12"
    assert metadata["fat_tree_k"] == 12
    assert metadata["management"]["ipv4_subnet"] == "172.20.20.0/23"
    assert metadata["scale"]["num_core"] == 36
    assert metadata["scale"]["num_agg"] == 72
    assert metadata["scale"]["num_edge"] == 72
    assert metadata["scale"]["clients_per_edge"] == 2
    assert metadata["scale"]["total_clients"] == 144
    assert metadata["scale"]["host_density"] == "sparse"
    assert metadata["scale"]["full_density_clients_per_edge"] == 6
    assert metadata["pingmesh"]["coverage_epoch_cycles"] == 36
    assert metadata["pingmesh"]["coverage_epoch_seconds"] == 108
    assert len(metadata["devices"]["clients"]) == 144
    assert manifest.facts.clients_per_attached_switch == 2
    assert manifest.facts.host_density == "sparse"
    assert manifest.routing.ecmp_hash_policy_by_role == {
        DeviceRole.CORE: 1,
        DeviceRole.AGG: 0,
        DeviceRole.EDGE: 1,
    }
    assert len(manifest.links) == 1008

    for device in ("core1", "core36", "agg1", "agg72", "edge1", "edge72"):
        _assert_preseed_artifacts(tmp_path, device)

    core1 = _load_config(tmp_path, "core1")
    agg1 = _load_config(tmp_path, "agg1")
    edge1 = _load_config(tmp_path, "edge1")
    assert len([key for key in core1["INTERFACE"] if "|" not in key]) == 12
    assert len([key for key in agg1["INTERFACE"] if "|" not in key]) == 12
    assert len([key for key in edge1["INTERFACE"] if "|" not in key]) == 8

    network_counts: dict[ipaddress.IPv4Network, int] = {}
    devices = [
        *(f"core{i}" for i in range(1, 37)),
        *(f"agg{i}" for i in range(1, 73)),
        *(f"edge{i}" for i in range(1, 73)),
    ]
    for device in devices:
        config_path = tmp_path / "configs" / "sonic" / device / "config_db.json"
        for network in interface_networks_for_config(config_path).values():
            parsed = ipaddress.ip_network(network)
            if parsed.subnet_of(ipaddress.ip_network("10.0.0.0/8")):
                assert parsed.prefixlen == 30
                network_counts[parsed] = network_counts.get(parsed, 0) + 1

    assert len(network_counts) == 864
    assert set(network_counts.values()) == {2}
