from datetime import UTC, datetime

from examples.agents.diagnostic_harness.config import DiagnosabilityConfig
from examples.agents.diagnostic_harness.hypotheses import DiagnosabilityGate, HypothesisScorer
from examples.agents.diagnostic_harness.models import Evidence, RankedInterfaceCandidate


def _evidence(
    evidence_id,
    category,
    value,
    source,
    *,
    entity_type="path",
    entity_id="client1--client2",
    **kwargs,
):
    return Evidence(
        evidence_id=evidence_id,
        entity_type=entity_type,
        entity_id=entity_id,
        category=category,
        value=value,
        source=source,
        timestamp=datetime.now(UTC),
        reliability=1.0,
        **kwargs,
    )


def _candidate(score=0.8):
    return RankedInterfaceCandidate(
        link_id="leaf1:Ethernet0--spine1:Ethernet0",
        primary_device="leaf1",
        primary_interface="Ethernet0",
        peer_device="spine1",
        peer_interface="Ethernet0",
        score=score,
    )


def test_mtu_threshold_and_peer_difference_outscore_other_impairments():
    evidence = [
        _evidence("E-size", "packet_size_threshold", {"size_dependent_failure": True}, "pingmesh_episode"),
        _evidence(
            "E-config",
            "configuration_difference",
            {"different": True, "field": "mtu"},
            "get_device_interfaces",
            entity_type="interface",
            entity_id="leaf1:Ethernet0",
        ),
    ]

    hypotheses = HypothesisScorer().score(evidence, interface_candidate=_candidate())

    assert max(hypotheses.values(), key=lambda item: item.probability).fault_type == "mtu_mismatch"
    assert hypotheses["mtu_mismatch"].missing_evidence == []


def test_causal_mtu_pair_outvotes_downstream_loss_without_lowering_gate():
    candidate = _candidate()
    evidence = [
        _evidence(
            "E-size",
            "packet_size_threshold",
            {
                "size_dependent_failure": True,
                "largest_successful_payload_size": 1372,
                "smallest_failed_payload_size": 1400,
            },
            "ping_test_df_size_sweep",
            probe_id="mtu-sweep",
            possible_paths=((candidate.link_id,),),
        ),
        _evidence(
            "E-config",
            "configuration_difference",
            {"different": True, "field": "mtu", "local_mtu": 1400, "peer_mtu": 9100},
            "get_device_interfaces",
            entity_type="interface",
            entity_id=f"{candidate.primary_device}:{candidate.primary_interface}",
            independence_key="config-read",
        ),
    ]
    evidence.extend(
        _evidence(
            f"E-loss-{index}",
            "packet_loss_rate",
            0.25,
            "ping_test_repeated",
            independence_key=f"loss-{index}",
        )
        for index in range(3)
    )

    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)
    decision = DiagnosabilityGate().analyze(hypotheses, evidence, [candidate])

    assert hypotheses["mtu_mismatch"].probability >= DiagnosabilityConfig().submit_confidence
    assert hypotheses["mtu_mismatch"].score - hypotheses["packet_loss"].score >= 2.0
    assert decision.can_submit
    assert decision.top_hypothesis_id == "H-mtu_mismatch"


def test_unrelated_peer_mtu_difference_does_not_dominate_packet_loss():
    candidate = _candidate()
    evidence = [
        _evidence(
            "E-size",
            "packet_size_threshold",
            {
                "size_dependent_failure": True,
                "largest_successful_payload_size": 1372,
                "smallest_failed_payload_size": 1400,
            },
            "ping_test_df_size_sweep",
            probe_id="mtu-sweep",
            possible_paths=((candidate.link_id,),),
        ),
        _evidence(
            "E-unrelated-config",
            "configuration_difference",
            {"different": True, "field": "mtu", "local_mtu": 1400, "peer_mtu": 9100},
            "get_device_interfaces",
            entity_type="interface",
            entity_id="leaf99:Ethernet99",
            independence_key="config-read",
        ),
    ]
    evidence.extend(
        _evidence(
            f"E-loss-{index}",
            "packet_loss_rate",
            0.25,
            "ping_test_repeated",
            independence_key=f"loss-{index}",
        )
        for index in range(4)
    )

    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    assert hypotheses["packet_loss"].score > hypotheses["mtu_mismatch"].score


def test_mtu_config_without_stable_success_failure_boundary_does_not_dominate_loss():
    candidate = _candidate()
    evidence = [
        _evidence(
            "E-ambiguous-size",
            "packet_size_threshold",
            {"size_dependent_failure": True, "smallest_failed_payload_size": 1400},
            "ping_test_df_size_sweep",
            probe_id="mtu-sweep",
            possible_paths=((candidate.link_id,),),
        ),
        _evidence(
            "E-config",
            "configuration_difference",
            {"different": True, "field": "mtu", "local_mtu": 1400, "peer_mtu": 9100},
            "get_device_interfaces",
            entity_type="interface",
            entity_id=f"{candidate.primary_device}:{candidate.primary_interface}",
            independence_key="config-read",
        ),
    ]
    evidence.extend(
        _evidence(
            f"E-loss-{index}",
            "packet_loss_rate",
            0.25,
            "ping_test_repeated",
            independence_key=f"loss-{index}",
        )
        for index in range(4)
    )

    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    assert hypotheses["packet_loss"].score > hypotheses["mtu_mismatch"].score


def test_tool_error_and_missing_observation_never_add_hypothesis_score():
    evidence = [
        _evidence("E-error", "tool_error", {"error": "timeout"}, "ping_test"),
        _evidence("E-missing", "missing_observation", {"supported": False}, "tool_registry"),
    ]

    hypotheses = HypothesisScorer().score(evidence)

    assert {hypothesis.score for hypothesis in hypotheses.values()} == {0.0}
    assert all(not hypothesis.supporting_evidence for hypothesis in hypotheses.values())


def test_correlated_rows_from_one_source_do_not_multiply_probability():
    one = [_evidence("E1", "packet_loss_rate", 0.2, "pingmesh_episode")]
    many = [
        _evidence(f"E{index}", "packet_loss_rate", 0.2, "pingmesh_episode", entity_id=f"c{index}--d")
        for index in range(1, 21)
    ]

    assert (
        HypothesisScorer().score(one)["packet_loss"].probability
        == HypothesisScorer().score(many)["packet_loss"].probability
    )


def test_pingmesh_views_of_one_window_do_not_outvote_checksum_failure():
    evidence = [
        _evidence(
            "E-episode",
            "packet_loss_rate",
            0.2,
            "pingmesh_episode",
            independence_key="public:pingmesh:episode",
        ),
        _evidence(
            "E-summary",
            "packet_loss_rate",
            0.2,
            "base_tool:get_pingmesh_summary",
            independence_key="tool:summary:call-1",
        ),
        _evidence(
            "E-hotspot",
            "packet_loss_rate",
            0.2,
            "base_tool:get_pingmesh_hotspots",
            independence_key="tool:hotspots:call-2",
        ),
        _evidence(
            "E-corruption",
            "payload_integrity_failure",
            True,
            "payload_integrity_link_test",
            probe_id="integrity-link",
        ),
    ]

    hypotheses = HypothesisScorer().score(evidence)

    assert hypotheses["packet_corruption"].score - hypotheses["packet_loss"].score >= 2.0
    assert max(hypotheses.values(), key=lambda item: item.probability).fault_type == "packet_corruption"


def test_gate_submits_mtu_only_with_direct_interface_and_independent_sources():
    candidate = _candidate()
    evidence = [
        _evidence("E-size", "packet_size_threshold", {"size_dependent_failure": True}, "pingmesh_episode"),
        _evidence(
            "E-config",
            "configuration_difference",
            {"different": True},
            "get_device_interfaces",
            entity_type="interface",
            entity_id="leaf1:Ethernet0",
        ),
    ]
    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    decision = DiagnosabilityGate(DiagnosabilityConfig(submit_confidence=0.5, submit_margin=0.1)).analyze(
        hypotheses, evidence, [candidate]
    )

    assert decision.can_submit
    assert decision.top_hypothesis_id == "H-mtu_mismatch"


def test_gate_keeps_packet_loss_inconclusive_without_corruption_exclusion():
    candidate = _candidate()
    evidence = [
        _evidence("E-episode", "packet_loss_rate", 0.2, "pingmesh_episode"),
        _evidence("E-probe", "packet_loss_rate", 0.3, "ping_test_repeated"),
        _evidence(
            "E-interface",
            "interface_counter_delta",
            {"in_errors": 3},
            "get_interface_metrics",
            entity_type="interface",
            entity_id="leaf1:Ethernet0",
        ),
    ]
    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    decision = DiagnosabilityGate().analyze(hypotheses, evidence, [candidate])

    assert not decision.can_submit
    assert "payload_integrity_valid" in decision.missing_requirements


def test_gate_accepts_strong_access_path_contrast_without_lowering_thresholds():
    candidate = RankedInterfaceCandidate(
        link_id="leaf1:Ethernet16--client1:eth1",
        primary_device="leaf1",
        primary_interface="Ethernet16",
        peer_device="client1",
        peer_interface="eth1",
        score=0.20,
        layer="access",
    )
    evidence = [
        _evidence(
            "E-passive-loss",
            "packet_loss_rate",
            0.20,
            "pingmesh_episode",
            independence_key="passive-window",
        ),
        _evidence(
            "E-access-contrast",
            "packet_loss_rate",
            0.20,
            "access_path_contrast",
            independence_key="composite-access-contrast",
            observed_path=(candidate.link_id,),
            possible_paths=((candidate.link_id,),),
            covered_links=(candidate.link_id,),
            path_observation_confidence=1.0,
            metadata={
                "selection": "access_path_contrast",
                "abnormal_flow_count": 4,
                "distinct_remote_endpoints": 3,
                "cleared_fabric_count": 4,
            },
        ),
        _evidence(
            "E-clean-checksum",
            "payload_integrity_failure",
            False,
            "payload_integrity_link_test",
            probe_id="access-checksum",
            independence_key="active-access-checksum",
            observed_path=(candidate.link_id,),
            possible_paths=((candidate.link_id,),),
            covered_links=(candidate.link_id,),
            path_observation_confidence=1.0,
        ),
    ]
    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    decision = DiagnosabilityGate().analyze(hypotheses, evidence, [candidate])

    assert decision.can_submit
    assert decision.confidence >= 0.75
    assert decision.margin >= 0.20


def test_unknown_ecmp_healthy_mtu_sweep_is_not_a_fault_level_contradiction():
    evidence = [
        _evidence(
            "E-healthy-sweep",
            "packet_size_threshold",
            {"size_dependent_failure": False},
            "ping_test_df_size_sweep",
            probe_id="mtu-packet-size-sweep",
            possible_paths=(("L1",), ("L2",), ("L3",), ("L4",)),
            path_observation_confidence=0.0,
        )
    ]

    hypotheses = HypothesisScorer().score(evidence, interface_candidate=_candidate())

    assert hypotheses["mtu_mismatch"].score == 0.0
    assert evidence[0].evidence_id not in hypotheses["mtu_mismatch"].contradicting_evidence


def test_exact_link_evidence_does_not_support_or_refute_another_candidate():
    candidate = _candidate()
    other_link = "leaf2:Ethernet0--spine2:Ethernet0"
    evidence = [
        _evidence(
            "E-other-loss",
            "packet_loss_rate",
            0.5,
            "base_tool:ping_link_test",
            covered_links=(other_link,),
            observed_path=(other_link,),
            path_observation_confidence=1.0,
        ),
        _evidence(
            "E-other-healthy-latency",
            "latency_median",
            1.0,
            "base_tool:latency_link_test",
            covered_links=(other_link,),
            observed_path=(other_link,),
            path_observation_confidence=1.0,
            metadata={"category_anomaly": False},
        ),
    ]

    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    assert "E-other-loss" not in hypotheses["packet_loss"].supporting_evidence
    assert "E-other-healthy-latency" not in hypotheses["high_latency"].contradicting_evidence


def test_gate_labels_zero_evidence_mass_as_unknown():
    hypotheses = HypothesisScorer().score([], interface_candidate=_candidate())

    decision = DiagnosabilityGate().analyze(hypotheses, [], [_candidate()])

    assert not decision.can_submit
    assert "positive_fault_evidence" in decision.missing_requirements


def test_low_recall_candidate_never_lowers_submission_interface_threshold():
    candidate = _candidate(score=0.08)
    evidence = [
        _evidence("E-size", "packet_size_threshold", {"size_dependent_failure": True}, "pingmesh_episode"),
        _evidence(
            "E-config",
            "configuration_difference",
            {"different": True},
            "get_device_interfaces",
            entity_type="interface",
            entity_id="leaf1:Ethernet0",
        ),
    ]
    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    decision = DiagnosabilityGate(DiagnosabilityConfig(submit_confidence=0.5, submit_margin=0.1)).analyze(
        hypotheses, evidence, [candidate]
    )

    assert not decision.can_submit
    assert "ranked_interface" in decision.missing_requirements


def test_unique_active_latency_path_does_not_invent_fault_endpoint_for_gate():
    candidate = _candidate(score=0.8)
    evidence = [
        _evidence(
            "E-episode-latency",
            "latency_p95",
            60.0,
            "pingmesh_episode",
            metadata={"absolute_anomaly": True},
        ),
        _evidence(
            "E-link-isolation",
            "latency_median",
            100.0,
            "ping_test_rtt_matrix",
            probe_id="rtt-matrix",
            covered_links=(candidate.link_id,),
            observed_path=(candidate.link_id,),
            path_observation_confidence=1.0,
            metadata={"absolute_anomaly": True, "selection": "link_isolation"},
        ),
    ]
    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    decision = DiagnosabilityGate().analyze(hypotheses, evidence, [candidate])

    assert not decision.can_submit
    assert "direct_interface_evidence" in decision.missing_requirements
    assert decision.confidence >= DiagnosabilityConfig().submit_confidence
    assert decision.margin >= DiagnosabilityConfig().submit_margin


def test_one_way_latency_endpoint_evidence_satisfies_interface_gate():
    candidate = _candidate(score=0.8)
    evidence = [
        _evidence(
            "E-episode-latency",
            "latency_p95",
            60.0,
            "pingmesh_episode",
            metadata={"absolute_anomaly": True},
        ),
        _evidence(
            "E-one-way",
            "latency_median",
            120.0,
            "latency_link_test",
            probe_id="directional-link-latency",
            covered_links=(candidate.link_id,),
            observed_path=(candidate.link_id,),
            path_observation_confidence=1.0,
            independence_key="probe:directional-link-latency:direction:1",
            metadata={
                "absolute_anomaly": True,
                "category_anomaly": True,
                "selection": "directional_link_latency",
                "fault_endpoint_device": "leaf1",
                "fault_endpoint_interface": "Ethernet0",
            },
        ),
    ]
    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    decision = DiagnosabilityGate().analyze(hypotheses, evidence, [candidate])

    assert decision.can_submit
    assert decision.top_hypothesis_id == "H-high_latency"


def test_corruption_direction_requires_independent_exact_link_corroboration():
    candidate = _candidate(score=0.8)
    directional = _evidence(
        "E-corruption-direction",
        "payload_integrity_failure",
        True,
        "payload_integrity_link_test",
        probe_id="payload-integrity",
        independence_key="probe:payload-integrity:1",
        covered_links=(candidate.link_id,),
        observed_path=(candidate.link_id,),
        path_observation_confidence=1.0,
        metadata={
            "selection": "link_integrity",
            "fault_endpoint_device": candidate.primary_device,
            "fault_endpoint_interface": candidate.primary_interface,
        },
    )
    passive_loss = _evidence(
        "E-passive-loss",
        "packet_loss_rate",
        0.30,
        "pingmesh_episode",
        independence_key="episode-loss-window",
    )
    evidence = [directional, passive_loss]
    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    decision = DiagnosabilityGate().analyze(hypotheses, evidence, [candidate])

    assert not decision.can_submit
    assert "corruption_endpoint_corroboration" in decision.missing_requirements

    evidence.append(
        _evidence(
            "E-exact-link-loss",
            "packet_loss_rate",
            0.25,
            "ping_test_repeated",
            probe_id="repeated-loss",
            independence_key="probe:repeated-loss:1",
            covered_links=(candidate.link_id,),
            observed_path=(candidate.link_id,),
            path_observation_confidence=1.0,
            metadata={"rounds": 2, "sent": 40},
        )
    )
    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    corroborated = DiagnosabilityGate().analyze(hypotheses, evidence, [candidate])

    assert corroborated.can_submit
    assert corroborated.top_hypothesis_id == "H-packet_corruption"


def test_gate_rejects_impairment_when_direct_semantic_fault_is_unexplained():
    candidate = _candidate(score=0.8)
    evidence = [
        _evidence(
            "E-latency",
            "latency_p95",
            80.0,
            "pingmesh_episode",
            metadata={"absolute_anomaly": True},
        ),
        _evidence(
            "E-directional",
            "latency_median",
            90.0,
            "latency_link_test",
            probe_id="directional-link-latency",
            covered_links=(candidate.link_id,),
            observed_path=(candidate.link_id,),
            path_observation_confidence=1.0,
            metadata={
                "absolute_anomaly": True,
                "category_anomaly": True,
                "fault_endpoint_device": candidate.primary_device,
                "fault_endpoint_interface": candidate.primary_interface,
            },
        ),
        _evidence(
            "E-policy",
            "configuration_difference",
            {"difference": "explicit_prefix_deny"},
            "get_device_config",
            entity_type="device",
            entity_id="leaf2",
            metadata={"semantic_family": "route_policy", "direct_configuration_evidence": True},
        ),
    ]
    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    decision = DiagnosabilityGate().analyze(hypotheses, evidence, [candidate])

    assert not decision.can_submit
    assert "unexplained_strong_evidence" in decision.missing_requirements


def test_gate_ignores_planning_only_semantic_claim_as_residual_fault():
    candidate = _candidate(score=0.8)
    evidence = [
        _evidence(
            "E-latency",
            "latency_p95",
            80.0,
            "pingmesh_episode",
            metadata={"absolute_anomaly": True},
        ),
        _evidence(
            "E-directional",
            "latency_median",
            90.0,
            "latency_link_test",
            probe_id="directional-link-latency",
            covered_links=(candidate.link_id,),
            observed_path=(candidate.link_id,),
            path_observation_confidence=1.0,
            metadata={
                "absolute_anomaly": True,
                "category_anomaly": True,
                "fault_endpoint_device": candidate.primary_device,
                "fault_endpoint_interface": candidate.primary_interface,
            },
        ),
        _evidence(
            "E-policy-claim",
            "configuration_difference",
            {"difference": "possible"},
            "base_diagnosis_text",
            entity_type="device",
            entity_id="leaf2",
            metadata={"semantic_family": "route_policy"},
            supports_submission=False,
        ),
    ]
    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    decision = DiagnosabilityGate().analyze(hypotheses, evidence, [candidate])

    assert decision.can_submit


def test_small_repeated_loss_noise_does_not_support_any_fault_hypothesis():
    evidence = [
        _evidence(
            "E-random-noise",
            "packet_loss_rate",
            0.05,
            "ping_test_repeated",
            probe_id="repeated-packet-loss",
            metadata={"warning": False, "strong": False, "rounds": 2},
        )
    ]

    hypotheses = HypothesisScorer().score(evidence)

    assert {item.score for item in hypotheses.values()} == {0.0}
    assert all(not item.supporting_evidence for item in hypotheses.values())


def test_route_policy_configuration_difference_is_not_impairment_support():
    evidence = [
        _evidence(
            "E-policy",
            "configuration_difference",
            {"semantic_family": "route_policy", "prefix": "192.0.2.0/30"},
            "base_diagnosis_text",
            entity_type="device",
            entity_id="leaf1",
            metadata={"semantic_family": "route_policy", "direct_configuration_evidence": True},
        )
    ]
    hypotheses = HypothesisScorer().score(evidence)
    assert {item.score for item in hypotheses.values()} == {0.0}


def test_two_round_single_link_loss_without_direction_does_not_invent_endpoint():
    candidate = _candidate(score=0.8)
    evidence = [
        _evidence("E-episode-loss", "packet_loss_rate", 0.2, "pingmesh_episode"),
        _evidence(
            "E-link-loss",
            "packet_loss_rate",
            0.2,
            "ping_test_repeated",
            probe_id="repeated-packet-loss",
            covered_links=(candidate.link_id,),
            observed_path=(candidate.link_id,),
            path_observation_confidence=1.0,
            metadata={"warning": True, "strong": True, "rounds": 2, "selection": "link_isolation"},
        ),
        _evidence(
            "E-valid-checksum",
            "payload_integrity_failure",
            False,
            "payload_integrity_test",
            probe_id="payload-integrity",
        ),
    ]
    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    decision = DiagnosabilityGate().analyze(hypotheses, evidence, [candidate])

    assert not decision.can_submit
    assert "direct_interface_evidence" in decision.missing_requirements


def test_directional_missing_sequences_bind_loss_to_confirmed_endpoint():
    candidate = _candidate(score=0.8)
    evidence = [
        _evidence("E-episode-loss", "packet_loss_rate", 0.2, "pingmesh_episode"),
        _evidence(
            "E-link-loss",
            "packet_loss_rate",
            0.2,
            "ping_test_repeated",
            probe_id="repeated-packet-loss",
            covered_links=(candidate.link_id,),
            observed_path=(candidate.link_id,),
            path_observation_confidence=1.0,
            metadata={"warning": True, "strong": True, "rounds": 2, "selection": "link_isolation"},
        ),
        _evidence(
            "E-direction",
            "packet_loss_rate",
            0.2,
            "payload_integrity_link_test",
            probe_id="payload-integrity-links",
            covered_links=(candidate.link_id,),
            observed_path=(candidate.link_id,),
            path_observation_confidence=1.0,
            metadata={
                "fault_endpoint_device": candidate.primary_device,
                "fault_endpoint_interface": candidate.primary_interface,
                "missing_sequences": 4,
            },
        ),
        _evidence("E-valid-checksum", "payload_integrity_failure", False, "payload_integrity_test"),
    ]
    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    decision = DiagnosabilityGate().analyze(hypotheses, evidence, [candidate])
    assert decision.can_submit
    assert decision.confidence >= 0.75
    assert decision.margin >= 0.20


def test_bounded_bidirectional_link_loss_is_direct_after_forty_packets():
    candidate = _candidate(score=0.8)
    evidence = [
        _evidence("E-episode-loss", "packet_loss_rate", 0.2, "pingmesh_episode"),
        _evidence(
            "E-link-loss",
            "packet_loss_rate",
            0.125,
            "payload_integrity_link_test",
            probe_id="payload-integrity-links",
            covered_links=(candidate.link_id,),
            observed_path=(candidate.link_id,),
            path_observation_confidence=1.0,
            metadata={
                "sent": 40,
                "fault_endpoint_device": candidate.primary_device,
                "fault_endpoint_interface": candidate.primary_interface,
            },
        ),
        _evidence(
            "E-valid-checksum",
            "payload_integrity_failure",
            False,
            "payload_integrity_link_test",
            probe_id="payload-integrity-links",
        ),
    ]
    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    decision = DiagnosabilityGate().analyze(hypotheses, evidence, [candidate])

    assert decision.can_submit
    assert decision.confidence >= 0.75
    assert decision.margin >= 0.20


def test_direct_link_checksum_failure_submits_corruption_without_lowering_gates():
    candidate = _candidate(score=0.8)
    evidence = [
        _evidence("E-episode-loss", "packet_loss_rate", 0.2, "pingmesh_episode"),
        _evidence(
            "E-end-to-end-corruption",
            "payload_integrity_failure",
            True,
            "payload_integrity_test",
            probe_id="payload-integrity",
        ),
        _evidence(
            "E-link-corruption",
            "payload_integrity_failure",
            True,
            "payload_integrity_link_test",
            probe_id="payload-integrity-links",
            covered_links=(candidate.link_id,),
            observed_path=(candidate.link_id,),
            possible_paths=((candidate.link_id,),),
            path_observation_confidence=1.0,
            metadata={
                "selection": "link_integrity",
                "checksum_failures": 3,
                "fault_endpoint_device": candidate.primary_device,
                "fault_endpoint_interface": candidate.primary_interface,
            },
        ),
    ]
    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    decision = DiagnosabilityGate().analyze(hypotheses, evidence, [candidate])

    assert decision.can_submit
    assert decision.top_hypothesis_id == "H-packet_corruption"
    assert decision.confidence >= 0.75
    assert decision.margin >= 0.20
