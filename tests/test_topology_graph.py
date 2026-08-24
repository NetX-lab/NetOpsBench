from examples.agents.diagnostic_harness.normalization.interface import LinkEndpoint, PhysicalLink, TopologyIndex
from examples.agents.diagnostic_harness.topology.graph import TopologyGraph, TopologyGraphLink


def _endpoint(device, interface, role):
    canonical = interface if role == "client" else f"Ethernet{(int(interface[3:]) - 1) * 4}"
    return LinkEndpoint(device, canonical, (interface, canonical))


def clos_graph(width: int = 2):
    roles = {
        "client1": "client",
        "client2": "client",
        "leaf1": "leaf",
        "leaf2": "leaf",
    }
    roles.update({f"spine{index}": "spine" for index in range(1, width + 1)})
    definitions = [("client1", "eth1", "leaf1", f"eth{width + 3}", "client-leaf")]
    definitions.extend(("leaf1", f"eth{index}", f"spine{index}", "eth1", "spine-leaf") for index in range(1, width + 1))
    definitions.extend((f"spine{index}", "eth2", "leaf2", f"eth{index}", "spine-leaf") for index in range(1, width + 1))
    definitions.append(("leaf2", f"eth{width + 3}", "client2", "eth1", "client-leaf"))
    physical = []
    graph_links = []
    for left, left_if, right, right_if, kind in definitions:
        endpoint_a = _endpoint(left, left_if, roles[left])
        endpoint_b = _endpoint(right, right_if, roles[right])
        link = PhysicalLink(
            f"{left}:{endpoint_a.canonical_interface}--{right}:{endpoint_b.canonical_interface}", endpoint_a, endpoint_b
        )
        physical.append(link)
        graph_links.append(TopologyGraphLink(link, kind, 9232))
    index = TopologyIndex(devices=roles, links=tuple(physical), source="test")
    return TopologyGraph(
        index=index,
        links=tuple(graph_links),
        addresses={"192.0.2.1": "client1", "192.0.2.2": "client2"},
        attachments={"client1": "leaf1", "client2": "leaf2"},
    )


def fat_tree_k12_graph():
    """Small projection that preserves k=12's 36 cross-pod ECMP paths."""
    roles = {"client-a": "client", "client-b": "client", "edge-a": "edge", "edge-b": "edge"}
    roles.update({f"agg-a-{index}": "agg" for index in range(6)})
    roles.update({f"agg-b-{index}": "agg" for index in range(6)})
    roles.update({f"core-{group}-{index}": "core" for group in range(6) for index in range(6)})
    definitions = [("client-a", "edge-a", "client-edge")]
    for group in range(6):
        source_agg = f"agg-a-{group}"
        destination_agg = f"agg-b-{group}"
        definitions.append(("edge-a", source_agg, "edge-agg"))
        definitions.append((destination_agg, "edge-b", "edge-agg"))
        for index in range(6):
            core = f"core-{group}-{index}"
            definitions.append((source_agg, core, "agg-core"))
            definitions.append((core, destination_agg, "agg-core"))
    definitions.append(("edge-b", "client-b", "client-edge"))

    ports = {device: 0 for device in roles}
    physical = []
    graph_links = []
    for left, right, kind in definitions:
        left_endpoint = LinkEndpoint(left, f"Ethernet{ports[left] * 4}", ())
        right_endpoint = LinkEndpoint(right, f"Ethernet{ports[right] * 4}", ())
        ports[left] += 1
        ports[right] += 1
        link = PhysicalLink(
            f"{left}:{left_endpoint.canonical_interface}--{right}:{right_endpoint.canonical_interface}",
            left_endpoint,
            right_endpoint,
        )
        physical.append(link)
        graph_links.append(TopologyGraphLink(link, kind, 9232))
    index = TopologyIndex(
        devices=roles,
        links=tuple(physical),
        attachment_devices=("edge-a", "edge-b"),
        source="fat-tree-k12-test",
    )
    return TopologyGraph(
        index=index,
        links=tuple(graph_links),
        addresses={"192.0.2.1": "client-a", "192.0.2.2": "client-b"},
        attachments={"client-a": "edge-a", "client-b": "edge-b"},
    )


def test_graph_enumerates_both_shortest_ecmp_paths():
    paths = clos_graph().all_shortest_paths("client1", "client2")

    assert len(paths) == 2
    assert all(len(path) == 4 for path in paths)
    assert any(any("spine1" in link for link in path) for path in paths)
    assert any(any("spine2" in link for link in path) for path in paths)


def test_graph_resolves_data_ip_and_preserves_canonical_endpoint_names():
    graph = clos_graph()

    paths = graph.all_shortest_paths("client1", "192.0.2.2")
    assert len(paths) == 2
    link = graph.endpoint_link("leaf1", "eth1")
    assert link is not None
    assert link.physical.endpoint_a.canonical_interface == "Ethernet0"


def test_graph_path_enumeration_is_bounded():
    assert len(clos_graph().all_shortest_paths("client1", "client2", max_paths=1)) == 1


def test_fat_tree_profile_counts_all_ecmp_paths_without_enumerating_them():
    graph = fat_tree_k12_graph()

    profile = graph.shortest_path_profile("client-a", "client-b", max_paths=4)

    assert profile.total_paths == 36
    assert len(profile.paths) == 4
    assert profile.truncated
    source_uplinks = graph.fabric_links("edge-a")
    assert len(source_uplinks) == 6
    assert {profile.link_fractions[link.link_id] for link in source_uplinks} == {1 / 6}
    assert sum(profile.link_fractions.values()) == 6


def test_fat_tree_shortest_path_union_includes_every_multitier_candidate_link():
    graph = fat_tree_k12_graph()

    link_union = graph.shortest_path_network_link_union("client-a", "client-b")

    assert len(link_union) == 84
    assert link_union == {
        link_id
        for link_id in graph.shortest_path_profile("client-a", "client-b", max_paths=0).link_fractions
        if graph.link(link_id) in graph.network_links()
    }


def test_functional_roles_and_probe_width_are_topology_derived():
    graph = fat_tree_k12_graph()

    assert graph.attachment_devices() == ("edge-a", "edge-b")
    assert set(graph.index.transit_devices()) == set(graph.roles) - {"client-a", "client-b", "edge-a", "edge-b"}
    assert graph.max_attachment_fabric_degree() == 6
    assert graph.probe_frontier_size(4, maximum=8) == 6
    assert graph.probe_frontier_size(4, maximum=5) == 5
    assert graph.failure_domain_probe_frontier_size(4, maximum=20) == 12
    assert graph.failure_domain_probe_frontier_size(4, maximum=8) == 8


def test_xlarge_clos_frontier_covers_both_sixteen_way_failure_domains():
    graph = clos_graph(width=16)

    assert graph.max_attachment_fabric_degree() == 16
    assert graph.failure_domain_probe_frontier_size(4, maximum=40) == 32
    assert len(graph.all_shortest_paths("client1", "client2", max_paths=32)) == 16
