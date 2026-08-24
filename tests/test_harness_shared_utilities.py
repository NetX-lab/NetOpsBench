import json
from datetime import UTC, datetime
from types import SimpleNamespace

from examples.agents.diagnostic_harness.context_topology import load_context_manifest
from examples.agents.diagnostic_harness.evidence.time import parse_timestamp
from examples.agents.diagnostic_harness.normalization.interface import TopologyIndex
from examples.agents.diagnostic_harness.topology.graph import TopologyGraph
from tests.diagnostic_harness_helpers import write_two_leaf_manifest


def test_context_topology_loader_is_shared_by_index_and_graph(tmp_path):
    manifest_path = write_two_leaf_manifest(tmp_path, topology_id="shared-inventory")
    context = SimpleNamespace(
        metadata={"worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": str(tmp_path)}},
        topology={},
    )

    manifest, source = load_context_manifest(context)
    index = TopologyIndex.from_context(context)
    graph = TopologyGraph.from_context(context)

    assert manifest is not None
    assert manifest.topology_id == "shared-inventory"
    assert source == str(manifest_path)
    assert index.source == source
    assert graph.index.source == source


def test_context_topology_loader_falls_back_to_public_projection(tmp_path):
    manifest_path = write_two_leaf_manifest(tmp_path, topology_id="public-projection")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    context = SimpleNamespace(metadata={}, topology=payload)

    manifest, source = load_context_manifest(context)

    assert manifest is not None
    assert manifest.topology_id == "public-projection"
    assert source == "context_manifest"
    assert load_context_manifest(context, include_context_payload=False) == (None, None)


def test_shared_timestamp_parser_normalizes_naive_and_zulu_values():
    expected = datetime(2026, 8, 20, 4, 3, 1, tzinfo=UTC)

    assert parse_timestamp("2026-08-20T04:03:01Z") == expected
    assert parse_timestamp("2026-08-20T04:03:01") == expected
    assert parse_timestamp("not-a-time") is None
