"""Contracts for the single offline compiler and its exact observation audit."""

from collections import Counter

import pytest

from netopsbench.platform.pingmesh.detector import AnomalyDetector
from netopsbench.platform.pingmesh.planner import compile_pingmesh_plan
from netopsbench.platform.topology.clos_builder import build_clos_plan
from netopsbench.platform.topology.config import TopologyConfig, config_for_scale
from netopsbench.platform.topology.fat_tree_builder import build_fat_tree_plan
from netopsbench.platform.topology.topology_utils import coerce_topology_manifest


@pytest.fixture(scope="module")
def manifests():
    result = {}
    for scale in ("xs", "medium", "large", "xlarge", "fat-tree-k8", "fat-tree-k12", "fat-tree-k18"):
        config = config_for_scale(scale)
        builder = build_clos_plan if isinstance(config, TopologyConfig) else build_fat_tree_plan
        result[scale] = builder(config).manifest
    return result


@pytest.mark.parametrize(
    "scale,pairs,epoch,window",
    [
        ("xs", 2, 2, 7),
        ("medium", 128, 3, 8),
        ("large", 1152, 24, 30),
        ("xlarge", 16512, 60, 66),
        ("fat-tree-k8", 4352, 36, 42),
        ("fat-tree-k12", 10368, 36, 42),
        ("fat-tree-k18", 26406, 36, 42),
    ],
)
def test_fixed_plans_cover_all_links_and_ports(manifests, scale, pairs, epoch, window):
    manifest = manifests[scale]
    plan = manifest.require_pingmesh_plan()
    assert plan.directed_pair_count == pairs
    assert plan.settings.coverage_epoch_seconds == epoch
    assert plan.settings.complete_window_seconds == window
    assert len(plan.coverage.directed_links) == 2 * len(manifest.links)
    assert set(plan.coverage.covered_switches) == {device.name for device in manifest.switches()}
    assert plan.coverage.minimum_witnesses >= (2 if scale in {"xlarge", "fat-tree-k18"} else 1)
    planned_pairs = {(client.name, destination) for client in plan.clients for destination in client.targets}
    assert all((destination, source) in planned_pairs for source, destination in planned_pairs)
    for client in plan.clients:
        assert len(client.frames) == plan.settings.coverage_epoch_cycles
        combinations = Counter()
        for frame in client.frames:
            combinations.update((client.targets[index], frame.port_batch_index) for index in frame.target_indices)
            socket_count = len(plan.settings.port_batches[frame.port_batch_index])
            assert len(frame.probes) == len(frame.target_indices) * (socket_count + 1)
            for target in range(len(frame.target_indices)):
                assert {socket for kind, index, socket in frame.probes if kind == "rtt" and index == target} == set(
                    range(socket_count)
                )
                assert sum(kind == "df" and index == target for kind, index, _ in frame.probes) == 1
        assert len(combinations) == len(client.targets) * len(plan.settings.port_batches)
        assert set(combinations.values()) == {1}
    if scale in {"xlarge", "fat-tree-k18"}:
        degrees = [len(client.targets) for client in plan.clients]
        assert max(degrees) - min(degrees) == 1


def test_compilation_is_deterministic_and_old_or_stale_artifacts_fail(manifests):
    manifest = manifests["medium"]
    assert compile_pingmesh_plan(manifest) == manifest.pingmesh_plan
    payload = manifest.model_dump(mode="json")
    payload["schema_version"] = "3"
    with pytest.raises(ValueError, match="Regenerate"):
        coerce_topology_manifest(payload)
    payload["schema_version"] = "4"
    payload["pingmesh_plan"]["clients"][0]["targets"].reverse()
    with pytest.raises(ValueError, match="fingerprint"):
        coerce_topology_manifest(payload)


def observation_rows(plan):
    rows = []
    for client in plan.clients:
        for cycle, frame in enumerate(client.frames):
            for index in frame.target_indices:
                rows.append(
                    {
                        "plan_id": plan.settings.plan_id,
                        "probe_cycle": cycle,
                        "src_name": client.name,
                        "dst_name": client.targets[index],
                        "destination_batch_index": frame.destination_batch_index,
                        "port_batch_index": frame.port_batch_index,
                        "rtt_ports_total": len(plan.settings.source_ports),
                        "rtt_ports_active": len(plan.settings.port_batches[frame.port_batch_index]),
                        "local_probe_errors": 0,
                    }
                )
    return rows


def test_audit_requires_exact_peers_frames_and_current_plan(manifests):
    manifest = manifests["medium"]
    detector = AnomalyDetector("http://localhost", "token", "org", "bucket", topology_metadata=manifest)
    rows = observation_rows(manifest.require_pingmesh_plan())
    assert detector.summarize_coverage(rows)["coverage_status"] == "complete"
    assert detector.summarize_coverage(rows[1:])["missing_pair_port_combinations"] == 1
    for replacement in ({"dst_name": "unknown"}, {"plan_id": "old"}, {"probe_cycle": 1}, {"local_probe_errors": 1}):
        altered = [dict(row) for row in rows]
        altered[0].update(replacement)
        assert detector.summarize_coverage(altered)["coverage_status"] == "incomplete"
    lossy = [dict(row, packets_sent=8, packets_lost=8, packet_loss=100) for row in rows]
    assert detector.summarize_coverage(lossy)["coverage_status"] == "complete"


def test_k18_addresses_asns_neighbors_and_management_capacity(manifests):
    manifest = manifests["fat-tree-k18"]
    assert len(manifest.switches()) == 405
    assert len(manifest.clients()) == 324
    assert len(manifest.links) == 3240
    assert len({device.asn for device in manifest.switches()}) == 405
    assert manifest.routing.core_asn_range == "65001-65081"
    assert manifest.routing.agg_asn_range == "65082-65243"
    assert manifest.routing.edge_asn_range == "65244-65405"
    assert manifest.clients()[-1].data_ip == "192.168.162.6"
    assert manifest.device("edge162").metadata["client_subnet"] == "192.168.162.0/24"
    config = config_for_scale("fat-tree-k18")
    fabric = build_fat_tree_plan(config)
    assert sum(len(device.bgp_neighbors) for device in fabric.device_plans) == 5832
