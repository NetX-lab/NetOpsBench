from dataclasses import replace
from datetime import UTC, datetime

from examples.agents.diagnostic_harness.config import TopologyRankerConfig
from examples.agents.diagnostic_harness.models import Evidence, EvidenceOrigin, RankedInterfaceCandidate
from examples.agents.diagnostic_harness.topology.interface_ranker import InterfaceRanker
from examples.agents.diagnostic_harness.topology.path_analysis import analyze_path_evidence, scope_path_evidence

from .test_topology_graph import clos_graph, fat_tree_k12_graph


def _path_evidence(evidence_id, value, *, probe_id=None):
    return Evidence(
        evidence_id=evidence_id,
        entity_type="path",
        entity_id="client1--client2",
        category="packet_loss_rate",
        value=value,
        source="ping_test" if probe_id else "pingmesh_episode",
        timestamp=datetime.now(UTC),
        reliability=1.0,
        probe_id=probe_id,
        metadata={"src_leaf": "leaf1", "dst_leaf": "leaf2"},
    )


def test_ranker_uses_path_evidence_without_assuming_one_ecmp_path():
    candidates = InterfaceRanker(clos_graph()).rank([_path_evidence("E1", 0.2)])

    assert len(candidates) == 4
    assert {candidate.primary_device for candidate in candidates} == {"leaf1", "leaf2"}
    # Each fabric link belongs to one of two possible ECMP paths. The client
    # attachment edges belong to both paths and remain the normalization peak.
    assert all(candidate.components["abnormal_path"] == 0.5 for candidate in candidates)


def test_xlarge_probe_expansion_covers_fabric_before_access_edges():
    graph = clos_graph(width=16)
    evidence = scope_path_evidence(graph, _path_evidence("E-wide", 0.2))
    ranker = InterfaceRanker(
        graph,
        TopologyRankerConfig(candidate_top_k=6),
        family="packet_loss",
    )

    selected = ranker.rank([evidence])
    expanded = ranker.expansion_candidates(
        [evidence],
        selected=selected,
        max_candidates=32,
    )

    assert len(expanded) == 32
    assert {item.link_id for item in expanded} == {item.link_id for item in graph.network_links()}
    assert all(item.layer == "fabric" for item in expanded)


def test_truncated_fat_tree_samples_retain_exact_ecmp_link_weights():
    graph = fat_tree_k12_graph()
    scoped = scope_path_evidence(
        graph,
        Evidence(
            "fat-tree-loss",
            "path",
            "client-a--client-b",
            "packet_loss_rate",
            0.2,
            "pingmesh_episode",
            datetime.now(UTC),
            metadata={"src_attachment": "edge-a", "dst_attachment": "edge-b"},
        ),
        max_paths=4,
    )

    analysis = analyze_path_evidence(graph, [scoped], config=TopologyRankerConfig(max_ecmp_paths=4))

    assert scoped.metadata["shortest_path_count"] == 36
    assert scoped.metadata["paths_truncated"] is True
    assert len(scoped.possible_paths) == 4
    assert {analysis.abnormal_support[link.link_id] for link in graph.fabric_links("edge-a")} == {1 / 6}


def test_failure_domain_is_attachment_local_and_fat_tree_pod_aware():
    graph = fat_tree_k12_graph()
    ranker = InterfaceRanker(graph, family="high_latency")
    evidence = [
        Evidence(
            "fat-tree-latency",
            "path",
            "client-a--client-b",
            "latency_p95",
            80.0,
            "pingmesh_episode",
            datetime.now(UTC),
            metadata={
                "src_attachment": "edge-a",
                "dst_attachment": "edge-b",
                "category_anomaly": True,
            },
        )
    ]

    source_domain = ranker.failure_domain_link_ids(evidence, max_attachments=1)

    assert {link.link_id for link in graph.fabric_links("edge-a")} <= source_domain
    assert any("core-" in link_id for link_id in source_domain)
    assert not {link.link_id for link in graph.fabric_links("edge-b")} <= source_domain


def test_failure_domain_ignores_healthy_and_tool_error_rows():
    graph = clos_graph(width=4)
    ranker = InterfaceRanker(graph, family="packet_loss")
    evidence = [
        Evidence(
            "healthy",
            "path",
            "client1--client2",
            "packet_loss_rate",
            0.0,
            "pingmesh_episode",
            datetime.now(UTC),
            metadata={"src_attachment": "leaf1", "dst_attachment": "leaf2"},
        ),
        Evidence(
            "error",
            "path",
            "client1--client2",
            "tool_error",
            "timeout",
            "ping_test",
            datetime.now(UTC),
            reliability=0.0,
        ),
    ]

    assert ranker.failure_domain_link_ids(evidence) == set()


def test_repeated_loss_to_one_client_admits_only_that_access_link():
    graph = clos_graph()
    rows = []
    for index, source in enumerate(("client2", "client3", "client4"), start=1):
        item = Evidence(
            evidence_id=f"E-access-{index}",
            entity_type="path",
            entity_id=f"{source}--client1",
            category="packet_loss_rate",
            value=1.0,
            source="pingmesh_episode",
            timestamp=datetime.now(UTC),
            independence_key=f"flow:{source}:client1",
            metadata={"src_name": source, "dst_name": "client1"},
        )
        rows.append(scope_path_evidence(graph, item))

    candidates = InterfaceRanker(graph, TopologyRankerConfig(candidate_top_k=6)).rank(rows)
    access = [candidate for candidate in candidates if candidate.layer == "access"]

    assert len(access) == 1
    assert access[0].primary_device == "leaf1"


def test_repeated_latency_to_one_client_admits_that_access_link():
    graph = clos_graph()
    rows = []
    for index, peer in enumerate(("client2", "client3", "client4"), start=1):
        item = Evidence(
            evidence_id=f"E-latency-{index}",
            entity_type="path",
            entity_id=f"client1--{peer}",
            category="latency_p95",
            value=90.0,
            source="pingmesh_episode",
            timestamp=datetime.now(UTC),
            metadata={
                "src_name": "client1",
                "dst_name": peer,
                "anomaly_type": "latency_spike",
            },
        )
        rows.append(scope_path_evidence(graph, item))

    candidates = InterfaceRanker(
        graph,
        TopologyRankerConfig(candidate_top_k=6),
        family="high_latency",
    ).rank(rows)
    access = [candidate for candidate in candidates if candidate.layer == "access"]

    assert len(access) == 1
    assert access[0].primary_device == "leaf1"
    assert access[0].primary_interface == "Ethernet16"


def test_healthy_active_probe_with_unknown_ecmp_path_does_not_contradict_all_members():
    graph = clos_graph()
    evidence = [
        scope_path_evidence(graph, _path_evidence("E1", 0.2)),
        scope_path_evidence(graph, _path_evidence("E2", 0.0, probe_id="P1")),
    ]
    candidates = InterfaceRanker(graph, TopologyRankerConfig(minimum_candidate_score=0.0)).rank(evidence)

    assert candidates
    assert all(candidate.components["healthy_path"] == 0.0 for candidate in candidates)


def test_healthy_fabric_cut_moves_probe_frontier_to_access_edge():
    graph = clos_graph()
    ranker = InterfaceRanker(graph, TopologyRankerConfig(candidate_top_k=6), family="packet_loss")
    fabric = []
    for index, link_id in enumerate(graph.all_shortest_paths("client1", "client2"), start=1):
        leaf_link = next(item for item in link_id if "leaf1" in item and "spine" in item)
        fabric.append(
            Evidence(
                f"healthy-fabric-{index}",
                "path",
                "spine--leaf1",
                "packet_loss_rate",
                0.0,
                "ping_test_repeated",
                datetime.now(UTC),
                probe_id="repeated-packet-loss",
                origin=EvidenceOrigin.ACTIVE_PROBE,
                metadata={"selection": "link_isolation"},
                observed_path=(leaf_link,),
                possible_paths=((leaf_link,),),
                covered_links=(leaf_link,),
                path_observation_confidence=1.0,
            )
        )
    frontier = ranker.healthy_link_isolation_frontier(
        fabric,
        warning_threshold=0.10,
        max_candidates=4,
        preferred_device="leaf1",
    )

    access = ranker.access_isolation_frontier(fabric, fabric_frontier=frontier, max_candidates=4)

    assert len(access) == 1
    assert access[0].layer == "access"
    assert access[0].primary_device == "leaf1"
    assert access[0].primary_interface == "Ethernet16"


def test_strong_endpoint_contrast_creates_auditable_access_link_evidence():
    graph = clos_graph()
    ranker = InterfaceRanker(graph, TopologyRankerConfig(candidate_top_k=6), family="packet_loss")
    access_link = graph.endpoint_link("leaf1", "Ethernet16").link_id
    evidence = []
    for index, remote in enumerate(("client2", "client3", "client4"), start=1):
        evidence.append(
            scope_path_evidence(
                graph,
                Evidence(
                    f"abnormal-{index}",
                    "path",
                    f"{remote}--client1",
                    "packet_loss_rate",
                    0.2,
                    "pingmesh_episode",
                    datetime.now(UTC),
                    reliability=0.3,
                    metadata={"src_name": remote, "dst_name": "client1"},
                ),
            )
        )
    for index, path in enumerate(graph.all_shortest_paths("client1", "client2"), start=1):
        link_id = next(item for item in path if "leaf1" in item and "spine" in item)
        evidence.append(
            Evidence(
                f"healthy-{index}",
                "path",
                "spine--leaf1",
                "packet_loss_rate",
                0.0,
                "ping_test_repeated",
                datetime.now(UTC),
                probe_id="repeated-packet-loss",
                origin=EvidenceOrigin.ACTIVE_PROBE,
                metadata={"selection": "link_isolation"},
                observed_path=(link_id,),
                possible_paths=((link_id,),),
                covered_links=(link_id,),
                path_observation_confidence=1.0,
            )
        )
    evidence.append(
        Evidence(
            "clean-checksum",
            "path",
            "leaf1--client1",
            "payload_integrity_failure",
            False,
            "payload_integrity_link_test",
            datetime.now(UTC),
            probe_id="payload-integrity-links",
            origin=EvidenceOrigin.ACTIVE_PROBE,
            observed_path=(access_link,),
            possible_paths=((access_link,),),
            covered_links=(access_link,),
            path_observation_confidence=1.0,
        )
    )

    derived = ranker.access_path_contrast_evidence(evidence, warning_threshold=0.10)

    assert len(derived) == 1
    assert derived[0].covered_links == (access_link,)
    assert derived[0].metadata["abnormal_flow_count"] == 3
    assert derived[0].metadata["distinct_remote_endpoints"] == 3
    assert derived[0].metadata["cleared_fabric_count"] == 2
    ranked = ranker.rank([*evidence, *derived])
    candidate = next(item for item in ranked if item.link_id == access_link)
    assert candidate.score >= 0.15


def test_access_path_contrast_rejects_one_off_endpoint_loss():
    graph = clos_graph()
    ranker = InterfaceRanker(graph, family="packet_loss")
    one_flow = [scope_path_evidence(graph, _path_evidence("one-off", 0.2))]

    assert ranker.access_path_contrast_evidence(one_flow, warning_threshold=0.10) == ()


def test_healthy_probe_on_one_observed_member_does_not_lower_another_member():
    graph = clos_graph()
    paths = graph.all_shortest_paths("client1", "client2")
    abnormal = scope_path_evidence(graph, _path_evidence("E1", 0.2))
    healthy = replace(
        scope_path_evidence(graph, _path_evidence("E2", 0.0, probe_id="P1")),
        observed_path=tuple(paths[0]),
        covered_links=tuple(paths[0]),
        path_observation_confidence=1.0,
    )
    config = TopologyRankerConfig(minimum_candidate_score=0.0, candidate_top_k=10)
    baseline = {item.link_id: item.score for item in InterfaceRanker(graph, config).rank([abnormal])}
    scoped = {item.link_id: item.score for item in InterfaceRanker(graph, config).rank([abnormal, healthy])}

    unobserved_network_links = set(paths[1]) - set(paths[0])
    unobserved_network_links &= set(baseline)
    assert unobserved_network_links
    assert all(scoped[link_id] == baseline[link_id] for link_id in unobserved_network_links)


def test_top_k_backfills_ecmp_members_below_candidate_threshold():
    config = TopologyRankerConfig(candidate_generation_threshold=0.30, candidate_top_k=6)
    candidates = InterfaceRanker(clos_graph(), config).rank([_path_evidence("E1", 0.2)])

    assert len(candidates) == 4
    assert all(candidate.score < config.candidate_generation_threshold for candidate in candidates)


def test_probe_only_expansion_covers_ecmp_member_omitted_by_top_k():
    graph = clos_graph()
    config = TopologyRankerConfig(candidate_generation_threshold=0.0, candidate_top_k=1)
    scoped = scope_path_evidence(graph, _path_evidence("E-ecmp", 0.2))
    ranker = InterfaceRanker(graph, config)
    selected = ranker.rank([scoped])

    expanded = ranker.expansion_candidates(
        [scoped],
        selected=selected,
        max_candidates=2,
    )

    assert len(selected) == 1
    assert expanded
    assert not {item.link_id for item in selected}.intersection(item.link_id for item in expanded)
    selected_path_members = {
        path_index for path_index, path in enumerate(scoped.possible_paths) if selected[0].link_id in path
    }
    assert any(
        {path_index for path_index, path in enumerate(scoped.possible_paths) if candidate.link_id in path}
        != selected_path_members
        for candidate in expanded
    )


def test_probe_only_expansion_never_adds_arbitrary_zero_support_link():
    graph = clos_graph()
    ranker = InterfaceRanker(graph, TopologyRankerConfig(candidate_top_k=1))

    assert ranker.expansion_candidates([], selected=[], max_candidates=4) == []


def test_probe_only_expansion_does_not_use_healthy_unknown_ecmp_path():
    graph = clos_graph()
    ranker = InterfaceRanker(graph, TopologyRankerConfig(candidate_top_k=1))
    healthy = scope_path_evidence(graph, _path_evidence("E-healthy-ecmp", 0.0, probe_id="healthy"))

    assert healthy.possible_paths
    assert ranker.expansion_candidates([healthy], selected=[], max_candidates=4) == []


def test_probe_only_expansion_does_not_repeat_abnormal_direct_link_probe():
    graph = clos_graph()
    ranker = InterfaceRanker(
        graph,
        TopologyRankerConfig(candidate_generation_threshold=0.0, candidate_top_k=1),
    )
    abnormal = scope_path_evidence(graph, _path_evidence("E-abnormal", 0.2))
    selected = ranker.rank([abnormal])
    previously_probed = next(
        link_id
        for path in abnormal.possible_paths
        for link_id in path
        if link_id != selected[0].link_id and graph.link(link_id) is not None
    )
    direct = Evidence(
        evidence_id="E-direct-control",
        entity_type="path",
        entity_id="direct-control",
        category="packet_loss_rate",
        value=0.2,
        source="ping_link_test",
        timestamp=datetime.now(UTC),
        reliability=1.0,
        probe_id="existing-link-probe",
        observed_path=(previously_probed,),
        possible_paths=((previously_probed,),),
        covered_links=(previously_probed,),
        path_observation_confidence=1.0,
    )

    expanded = ranker.expansion_candidates(
        [abnormal, direct],
        selected=selected,
        max_candidates=4,
    )

    assert previously_probed not in {item.link_id for item in expanded}


def test_checksum_frontier_reuses_preferred_leaf_exact_isolation_links():
    graph = clos_graph()
    healthy = []
    for link in graph.network_links():
        leaf = next(
            endpoint.device
            for endpoint in (link.physical.endpoint_a, link.physical.endpoint_b)
            if graph.roles.get(endpoint.device) == "leaf"
        )
        healthy.append(
            Evidence(
                evidence_id=f"healthy-{link.link_id}",
                entity_type="path",
                entity_id=link.link_id,
                category="packet_loss_rate",
                value=0.0,
                source="ping_link_test",
                timestamp=datetime.now(UTC),
                probe_id="loss-isolation",
                origin=EvidenceOrigin.ACTIVE_PROBE,
                metadata={"selection": "link_isolation", "suspect_leaf": leaf},
                observed_path=(link.link_id,),
                possible_paths=((link.link_id,),),
                covered_links=(link.link_id,),
                path_observation_confidence=1.0,
            )
        )

    frontier = InterfaceRanker(graph).healthy_link_isolation_frontier(
        healthy,
        warning_threshold=0.1,
        max_candidates=6,
        preferred_device="leaf2",
    )

    assert len(frontier) == 2
    assert {item.primary_device for item in frontier} == {"leaf2"}
    assert {item.link_id for item in frontier} == {
        link.link_id
        for link in graph.network_links()
        if "leaf2" in {link.physical.endpoint_a.device, link.physical.endpoint_b.device}
    }
    assert all(item.evidence_ids for item in frontier)


def test_loss_expansion_retries_selected_candidate_only_when_not_actually_observed():
    graph = clos_graph()
    ranker = InterfaceRanker(
        graph,
        TopologyRankerConfig(candidate_generation_threshold=0.0, candidate_top_k=1),
        family="packet_loss",
    )
    abnormal = scope_path_evidence(graph, _path_evidence("E-abnormal", 0.2))
    selected = ranker.rank([abnormal])

    pending = ranker.expansion_candidates([abnormal], selected=selected, max_candidates=1)
    assert [item.link_id for item in pending] == [selected[0].link_id]

    direct = Evidence(
        evidence_id="E-direct",
        entity_type="path",
        entity_id=selected[0].link_id,
        category="packet_loss_rate",
        value=0.0,
        source="ping_link_test",
        timestamp=datetime.now(UTC),
        reliability=1.0,
        probe_id="link-breadth",
        observed_path=(selected[0].link_id,),
        possible_paths=((selected[0].link_id,),),
        covered_links=(selected[0].link_id,),
        path_observation_confidence=1.0,
        origin=EvidenceOrigin.ACTIVE_PROBE,
    )
    after_observation = ranker.expansion_candidates(
        [abnormal, direct],
        selected=selected,
        max_candidates=1,
    )
    assert not after_observation or after_observation[0].link_id != selected[0].link_id


def test_leaf_aggregate_expansion_moves_to_access_after_all_uplinks_are_healthy():
    graph = clos_graph()
    aggregate_rows = [
        scope_path_evidence(
            graph,
            Evidence(
                evidence_id=f"E-leaf-hotspot-{index}",
                entity_type="path",
                entity_id="leaf1--leaf2",
                category="packet_loss_rate",
                value=0.02,
                source="base_tool:get_pingmesh_hotspots",
                timestamp=datetime.now(UTC),
                metadata={
                    "src_leaf": "leaf1",
                    "dst_leaf": "leaf2",
                    "leaf_aggregate": True,
                    "weak_performance_symptom": True,
                },
            ),
        )
        for index in range(3)
    ]
    healthy_uplinks = []
    for link in graph.network_links():
        if "leaf1" not in {link.physical.endpoint_a.device, link.physical.endpoint_b.device}:
            continue
        healthy_uplinks.append(
            Evidence(
                evidence_id=f"healthy-{link.link_id}",
                entity_type="path",
                entity_id=link.link_id,
                category="packet_loss_rate",
                value=0.0,
                source="ping_link_test",
                timestamp=datetime.now(UTC),
                probe_id="link-isolation",
                observed_path=(link.link_id,),
                possible_paths=((link.link_id,),),
                covered_links=(link.link_id,),
                path_observation_confidence=1.0,
                origin=EvidenceOrigin.ACTIVE_PROBE,
            )
        )
    ranker = InterfaceRanker(graph, TopologyRankerConfig(candidate_top_k=1))

    expanded = ranker.expansion_candidates(
        [*aggregate_rows, *healthy_uplinks],
        selected=[],
        max_candidates=2,
    )

    assert expanded
    assert expanded[0].layer == "access"
    assert expanded[0].primary_device == "leaf1"


def test_mtu_expansion_includes_access_layer_without_polluting_submission_top_k():
    graph = clos_graph()
    threshold = Evidence(
        evidence_id="E-access-mtu",
        entity_type="path",
        entity_id="client1--client2",
        category="packet_size_threshold",
        value={"size_dependent_failure": True},
        source="ping_test_df_size_sweep",
        timestamp=datetime.now(UTC),
        probe_id="mtu-sweep",
        metadata={"source": "client1", "destination": "client2"},
    )
    threshold = scope_path_evidence(graph, threshold)
    ranker = InterfaceRanker(graph, TopologyRankerConfig(candidate_top_k=4))
    selected = ranker.rank([threshold])

    expanded = ranker.expansion_candidates([threshold], selected=selected, max_candidates=2)

    assert selected and all(candidate.layer == "fabric" for candidate in selected)
    assert expanded and all(candidate.layer == "access" for candidate in expanded)
    assert {candidate.primary_device for candidate in expanded} == {"leaf1", "leaf2"}


def test_mtu_expansion_prioritizes_active_sweep_over_passive_access_noise():
    graph = clos_graph()
    passive = Evidence(
        evidence_id="E-passive-mtu",
        entity_type="path",
        entity_id="client3--client4",
        category="packet_size_threshold",
        value={"size_dependent_failure": True},
        source="pingmesh_episode",
        timestamp=datetime.now(UTC),
        origin=EvidenceOrigin.PUBLIC_OBSERVATION,
    )
    active = Evidence(
        evidence_id="E-active-mtu",
        entity_type="path",
        entity_id="client1--client2",
        category="packet_size_threshold",
        value={"size_dependent_failure": True},
        source="ping_test_df_size_sweep",
        timestamp=datetime.now(UTC),
        probe_id="mtu-sweep",
        origin=EvidenceOrigin.ACTIVE_PROBE,
    )
    passive = scope_path_evidence(graph, passive)
    active = scope_path_evidence(graph, active)
    ranker = InterfaceRanker(graph, TopologyRankerConfig(candidate_top_k=1))
    selected = ranker.rank([passive, active])

    expanded = ranker.expansion_candidates([passive, active], selected=selected, max_candidates=2)

    active_access = {
        link_id
        for path in active.possible_paths
        for link_id in path
        if "client"
        in {
            graph.roles.get(graph.link(link_id).physical.endpoint_a.device),
            graph.roles.get(graph.link(link_id).physical.endpoint_b.device),
        }
    }
    assert {candidate.link_id for candidate in expanded} == active_access


def test_explicit_fault_direction_selects_endpoint_of_confirmed_link():
    graph = clos_graph()
    evidence = Evidence(
        evidence_id="E-direction",
        entity_type="path",
        entity_id="spine1--leaf2",
        category="payload_integrity_failure",
        value=True,
        source="payload_integrity_link_test",
        timestamp=datetime.now(UTC),
        probe_id="integrity",
        observed_path=("spine1:Ethernet4--leaf2:Ethernet0",),
        possible_paths=(("spine1:Ethernet4--leaf2:Ethernet0",),),
        covered_links=("spine1:Ethernet4--leaf2:Ethernet0",),
        path_observation_confidence=1.0,
        metadata={
            "source": "spine1",
            "destination": "leaf2",
            "selection": "link_integrity",
            "fault_endpoint_device": "spine1",
            "fault_endpoint_interface": "Ethernet4",
        },
    )

    candidate = InterfaceRanker(graph).rank([evidence])[0]

    assert (candidate.primary_device, candidate.primary_interface) == ("spine1", "Ethernet4")
    assert candidate.endpoint_confidence == 1.0


def test_explicit_fault_direction_overrides_wrong_initial_peer_endpoint():
    graph = clos_graph()
    evidence = Evidence(
        evidence_id="E-direction",
        entity_type="path",
        entity_id="spine1--leaf2",
        category="payload_integrity_failure",
        value=True,
        source="payload_integrity_link_test",
        timestamp=datetime.now(UTC),
        probe_id="integrity",
        observed_path=("spine1:Ethernet4--leaf2:Ethernet0",),
        possible_paths=(("spine1:Ethernet4--leaf2:Ethernet0",),),
        covered_links=("spine1:Ethernet4--leaf2:Ethernet0",),
        path_observation_confidence=1.0,
        metadata={
            "source": "spine1",
            "destination": "leaf2",
            "selection": "link_integrity",
            "fault_endpoint_device": "leaf2",
            "fault_endpoint_interface": "Ethernet0",
        },
    )

    candidate = InterfaceRanker(graph).rank(
        [evidence],
        initial_device="spine1",
        initial_interface="Ethernet4",
    )[0]

    assert (candidate.primary_device, candidate.primary_interface) == ("leaf2", "Ethernet0")
    assert candidate.endpoint_confidence == 1.0


def test_directional_fault_endpoint_outranks_repeated_peer_counter_symptoms():
    graph = clos_graph()
    link_id = "spine1:Ethernet4--leaf2:Ethernet0"
    directional = Evidence(
        evidence_id="E-direction",
        entity_type="path",
        entity_id="spine1--leaf2",
        category="payload_integrity_failure",
        value=True,
        source="payload_integrity_link_test",
        timestamp=datetime.now(UTC),
        probe_id="integrity",
        observed_path=(link_id,),
        possible_paths=((link_id,),),
        covered_links=(link_id,),
        path_observation_confidence=1.0,
        metadata={
            "source": "spine1",
            "destination": "leaf2",
            "selection": "link_integrity",
            "fault_endpoint_device": "leaf2",
            "fault_endpoint_interface": "Ethernet0",
        },
    )
    receiver_counters = [
        Evidence(
            evidence_id=f"E-counter-{index}",
            entity_type="interface",
            entity_id="spine1:Ethernet4",
            category="interface_counter_delta",
            value={"in_discards": 20 + index},
            source="get_interface_metrics",
            timestamp=datetime.now(UTC),
            independence_key="same-interface-window",
        )
        for index in range(2)
    ]

    candidate = InterfaceRanker(graph).rank([*receiver_counters, directional])[0]

    assert candidate.link_id == link_id
    assert (candidate.primary_device, candidate.primary_interface) == ("leaf2", "Ethernet0")
    assert (candidate.peer_device, candidate.peer_interface) == ("spine1", "Ethernet4")
    assert candidate.endpoint_confidence == 1.0


def test_directional_access_latency_is_admitted_and_outranks_indirect_paths():
    graph = clos_graph()
    link_id = "client1:eth1--leaf1:Ethernet16"
    direct = Evidence(
        evidence_id="E-access-direction",
        entity_type="path",
        entity_id="leaf1--client1",
        category="latency_median",
        value=90.0,
        source="latency_link_test",
        timestamp=datetime.now(UTC),
        reliability=0.6,
        probe_id="directional-link-latency",
        observed_path=(link_id,),
        possible_paths=((link_id,),),
        covered_links=(link_id,),
        path_observation_confidence=1.0,
        metadata={
            "selection": "directional_link_latency",
            "category_anomaly": True,
            "fault_endpoint_device": "leaf1",
            "fault_endpoint_interface": "Ethernet16",
        },
    )
    indirect = scope_path_evidence(
        graph,
        replace(
            _path_evidence("E-indirect", 0.2),
            category="latency_p95",
            value=80.0,
            metadata={
                "source": "client1",
                "destination": "client2",
                "category_anomaly": True,
            },
        ),
    )

    candidates = InterfaceRanker(graph, family="high_latency").rank([indirect, direct])

    assert candidates[0].link_id == link_id
    assert (candidates[0].primary_device, candidates[0].primary_interface) == ("leaf1", "Ethernet16")
    assert candidates[0].endpoint_confidence == 1.0


def test_peer_configuration_difference_binds_to_one_physical_link_and_endpoint():
    graph = clos_graph()
    evidence = Evidence(
        evidence_id="E-mtu",
        entity_type="interface",
        entity_id="spine2:Ethernet4",
        category="configuration_difference",
        value={"different": True, "local_mtu": 1400, "peer_mtu": 9232},
        source="get_device_interfaces",
        timestamp=datetime.now(UTC),
        reliability=1.0,
    )

    candidates = InterfaceRanker(graph).rank([evidence])

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.primary_device == "spine2"
    assert candidate.primary_interface == "Ethernet4"
    assert candidate.peer_device == "leaf2"
    assert candidate.peer_interface == "Ethernet4"
    assert candidate.evidence_ids == ("E-mtu",)


def test_initial_device_orders_the_correct_side_without_guessing_peer_name():
    evidence = replace(_path_evidence("E1", 0.2), metadata={})
    candidates = InterfaceRanker(clos_graph()).rank([evidence], initial_device="spine1")

    spine_candidates = [candidate for candidate in candidates if candidate.primary_device == "spine1"]
    assert len(spine_candidates) == 2
    assert {candidate.peer_device for candidate in spine_candidates} == {"leaf1", "leaf2"}


def test_exact_initial_interface_ranks_its_manifest_link_first():
    candidates = InterfaceRanker(clos_graph()).rank(
        [_path_evidence("E1", 0.2)],
        initial_device="spine1",
        initial_interface="Ethernet0",
    )

    assert candidates[0].primary_device == "spine1"
    assert candidates[0].primary_interface == "Ethernet0"


def test_single_topology_path_is_recorded_as_covered_not_unresolved_ecmp():
    graph = clos_graph()
    evidence = Evidence(
        evidence_id="E-isolation",
        entity_type="path",
        entity_id="spine2--192.0.2.2",
        category="latency_median",
        value=100.0,
        source="ping_test_rtt_matrix",
        timestamp=datetime.now(UTC),
        reliability=1.0,
        probe_id="rtt-matrix",
        metadata={"source": "spine2", "destination": "192.0.2.2", "absolute_anomaly": True},
    )

    scoped = scope_path_evidence(graph, evidence)

    assert len(scoped.possible_paths) == 1
    assert scoped.observed_path == scoped.possible_paths[0]
    assert scoped.covered_links == scoped.possible_paths[0]
    assert scoped.path_observation_confidence == 1.0


def test_single_link_repeated_loss_maps_to_one_canonical_interface_candidate():
    graph = clos_graph()
    evidence = Evidence(
        evidence_id="E-link-loss",
        entity_type="path",
        entity_id="spine2--192.0.2.2",
        category="packet_loss_rate",
        value=0.2,
        source="ping_test_repeated",
        timestamp=datetime.now(UTC),
        reliability=1.0,
        probe_id="repeated-packet-loss",
        metadata={
            "source": "spine2",
            "destination": "192.0.2.2",
            "source_leaf": "spine2",
            "destination_leaf": "leaf2",
            "warning": True,
            "strong": True,
            "rounds": 2,
            "selection": "link_isolation",
            "suspect_leaf": "leaf2",
        },
    )
    scoped = scope_path_evidence(graph, evidence)

    candidates = InterfaceRanker(graph).rank([scoped])

    assert candidates[0].link_id == "spine2:Ethernet4--leaf2:Ethernet4"
    assert candidates[0].primary_device == "leaf2"
    assert candidates[0].primary_interface == "Ethernet4"
    assert candidates[0].peer_device == "spine2"


def test_small_random_loss_on_unique_path_does_not_create_interface_candidate():
    graph = clos_graph()
    noise = Evidence(
        evidence_id="E-noise",
        entity_type="path",
        entity_id="spine2--192.0.2.2",
        category="packet_loss_rate",
        value=0.05,
        source="ping_test_repeated",
        timestamp=datetime.now(UTC),
        reliability=1.0,
        probe_id="repeated-packet-loss",
        metadata={
            "source": "spine2",
            "destination": "192.0.2.2",
            "warning": False,
            "strong": False,
            "rounds": 2,
        },
    )

    assert InterfaceRanker(graph).rank([scope_path_evidence(graph, noise)]) == []


def test_latency_ranking_does_not_treat_zero_loss_as_latency_contradiction():
    graph = clos_graph()
    link_id = "spine2:Ethernet4--leaf2:Ethernet4"
    latency = Evidence(
        evidence_id="E-latency",
        entity_type="path",
        entity_id="spine2--leaf2",
        category="latency_median",
        value=80.0,
        source="ping_test_rtt_matrix",
        timestamp=datetime.now(UTC),
        reliability=1.0,
        probe_id="rtt-link",
        observed_path=(link_id,),
        possible_paths=((link_id,),),
        covered_links=(link_id,),
        path_observation_confidence=1.0,
        origin=EvidenceOrigin.ACTIVE_PROBE,
        metadata={"category_anomaly": True, "selection": "link_isolation"},
    )
    no_loss = Evidence(
        evidence_id="E-no-loss",
        entity_type="path",
        entity_id="spine2--leaf2",
        category="packet_loss_rate",
        value=0.0,
        source="ping_link_test",
        timestamp=datetime.now(UTC),
        reliability=1.0,
        probe_id="loss-link",
        observed_path=(link_id,),
        possible_paths=((link_id,),),
        covered_links=(link_id,),
        path_observation_confidence=1.0,
        origin=EvidenceOrigin.ACTIVE_PROBE,
    )

    latency_candidate = InterfaceRanker(graph, family="high_latency").rank([latency, no_loss])[0]
    assert latency_candidate.link_id == link_id
    assert latency_candidate.components["healthy_path"] == 0.0


def test_latency_expansion_does_not_treat_exact_loss_probe_as_latency_coverage():
    graph = clos_graph()
    ranker = InterfaceRanker(
        graph,
        TopologyRankerConfig(candidate_generation_threshold=0.0, candidate_top_k=1),
        family="high_latency",
    )
    ambiguous = scope_path_evidence(
        graph,
        Evidence(
            evidence_id="E-latency-ambiguous",
            entity_type="path",
            entity_id="client1--client2",
            category="latency_p95",
            value=90.0,
            source="pingmesh_episode",
            timestamp=datetime.now(UTC),
            metadata={"category_anomaly": True},
        ),
    )
    selected = ranker.rank([ambiguous])
    loss_only = Evidence(
        evidence_id="E-loss-only",
        entity_type="path",
        entity_id=selected[0].link_id,
        category="packet_loss_rate",
        value=0.0,
        source="ping_link_test",
        timestamp=datetime.now(UTC),
        probe_id="loss-link",
        observed_path=(selected[0].link_id,),
        possible_paths=((selected[0].link_id,),),
        covered_links=(selected[0].link_id,),
        path_observation_confidence=1.0,
        origin=EvidenceOrigin.ACTIVE_PROBE,
    )

    expanded = ranker.expansion_candidates([ambiguous, loss_only], selected=selected, max_candidates=1)
    assert [item.link_id for item in expanded] == [selected[0].link_id]


def test_wide_equal_score_frontier_is_stratified_across_full_domain():
    candidates = [
        RankedInterfaceCandidate(
            link_id=f"link-{index:03d}",
            primary_device="leaf1",
            primary_interface=f"Ethernet{index}",
            peer_device=f"spine{index}",
            peer_interface="Ethernet0",
            score=0.2,
        )
        for index in range(100)
    ]

    ordered = InterfaceRanker._stratified_order(candidates, 5)

    assert [item.link_id for item in ordered[:5]] == [
        "link-000",
        "link-025",
        "link-050",
        "link-074",
        "link-099",
    ]
    assert len({item.link_id for item in ordered}) == 100
