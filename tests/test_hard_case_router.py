from datetime import UTC, datetime

import pytest

from examples.agents.diagnostic_harness.evidence import EvidenceStore
from examples.agents.diagnostic_harness.evidence.semantic import (
    evidence_from_diagnosis,
    extract_semantic_family_hints,
    has_route_policy_evidence,
)
from examples.agents.diagnostic_harness.models import (
    BaseAgentAssessment,
    BaseAgentStatus,
    DiagnosticState,
    Evidence,
    EvidenceOrigin,
    Hypothesis,
)
from examples.agents.diagnostic_harness.routing import HardCaseRouter

from .diagnostic_harness_helpers import diagnosis_result


def _structured(category: str) -> EvidenceStore:
    return EvidenceStore(
        [
            Evidence(
                evidence_id="E1",
                entity_type="path",
                entity_id="client1--client2",
                category=category,
                value=0.2,
                source="probe",
                timestamp=datetime.now(UTC),
            )
        ]
    )


@pytest.mark.parametrize(
    "fault_type",
    ["packet_loss", "packet_corruption", "mtu_mismatch", "high_latency"],
)
def test_impairment_families_always_escalate(fault_type):
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": fault_type,
            "device": "leaf1",
            "interface": "Ethernet0",
            "confidence": 0.95,
            "evidence": ["Direct but not discriminative impairment evidence."],
        }
    )

    decision = HardCaseRouter().route(result=result)

    assert not decision.fast_path
    assert "impairment_fault_family" in decision.reasons


def test_supported_easy_fault_and_healthy_result_take_fast_path():
    easy = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "device_down",
            "device": "leaf1",
            "interface": None,
            "confidence": 0.95,
            "evidence": ["The device alone is unreachable."],
        }
    )
    healthy = diagnosis_result(
        {
            "verdict": "network_healthy",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.95,
            "evidence": ["All bounded observations are healthy."],
        }
    )

    assert HardCaseRouter().route(result=easy).fast_path
    assert HardCaseRouter().route(result=healthy).fast_path


def test_large_healthy_negative_language_remains_fast_path():
    result = diagnosis_result(
        {
            "verdict": "network_healthy",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.95,
            "evidence": [
                "Pingmesh shows 0% packet loss on all 4,032 observed paths.",
                "BGP events show no non-established sessions.",
            ],
            "reasoning": "No anomalies were observed; all network paths show zero loss and healthy latency.",
        }
    )

    decision = HardCaseRouter().route(result=result)

    assert decision.fast_path
    assert decision.family is None


def test_recovered_bgp_transition_vetoes_healthy_branch_and_routes_temporal_verification():
    result = diagnosis_result(
        {
            "verdict": "network_healthy",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.95,
            "evidence": ["The BGP session is established now."],
        }
    )
    store = EvidenceStore(
        [
            Evidence(
                evidence_id="bgp-transition",
                entity_type="device",
                entity_id="leaf1",
                category="bgp_neighbor_state",
                value={
                    "event_type": "session_flap",
                    "states_observed": ["ACTIVE", "ESTABLISHED"],
                    "temporal_transition": True,
                },
                source="query_bgp_events",
                timestamp=datetime.now(UTC),
            )
        ]
    )
    assessment = BaseAgentAssessment(
        status=BaseAgentStatus.LOW_EVIDENCE,
        reasons=("public_observation_conflict",),
        direct_evidence=True,
        semantic_conflict=True,
    )

    decision = HardCaseRouter().route(
        result=result,
        evidence_store=store,
        base_assessment=assessment,
    )

    assert not decision.fast_path
    assert decision.family == "temporal_verification"


def test_valid_healthy_result_with_live_bgp_transition_routes_temporal_verification():
    result = diagnosis_result(
        {
            "verdict": "network_healthy",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.95,
            "evidence": ["All BGP sessions have recovered to ESTABLISHED."],
        }
    )
    store = EvidenceStore(
        [
            Evidence(
                evidence_id="live-bgp-flap",
                entity_type="device",
                entity_id="leaf82",
                category="bgp_neighbor_state",
                value={"event_type": "session_flap", "latest_state": "ESTABLISHED"},
                source="query_bgp_events",
                timestamp=datetime.now(UTC),
            )
        ]
    )
    assessment = BaseAgentAssessment(
        status=BaseAgentStatus.VALID,
        reasons=(),
        direct_evidence=True,
        semantic_conflict=False,
    )

    decision = HardCaseRouter().route(
        result=result,
        evidence_store=store,
        base_assessment=assessment,
    )

    assert not decision.fast_path
    assert decision.family == "temporal_verification"
    assert "structured_temporal_transition_requires_verification" in decision.reasons


def test_missing_bgp_route_consequence_does_not_claim_route_policy_root_cause():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf5",
            "interface": None,
            "confidence": 0.6,
            "evidence": [],
        }
    )
    store = EvidenceStore(
        [
            Evidence(
                evidence_id="missing-bgp-route",
                entity_type="device",
                entity_id="leaf5",
                category="route_presence",
                value={"prefix": "203.0.113.0/24", "present": False, "protocol": "bgp"},
                source="get_bgp_rib",
                timestamp=datetime.now(UTC),
                supports_submission=False,
                metadata={"missing_bgp_route": True, "route_semantic_candidate": True},
            ),
            Evidence(
                evidence_id="downstream-loss",
                entity_type="path",
                entity_id="client1--client5",
                category="packet_loss_rate",
                value=1.0,
                source="pingmesh_episode",
                timestamp=datetime.now(UTC),
                metadata={"source_attachment": "leaf1", "destination_attachment": "leaf5"},
            ),
        ]
    )
    assessment = BaseAgentAssessment(
        status=BaseAgentStatus.LOW_EVIDENCE,
        reasons=("missing_required_direct_evidence",),
        direct_evidence=False,
        semantic_conflict=False,
    )

    decision = HardCaseRouter().route(
        result=result,
        evidence_store=store,
        base_assessment=assessment,
    )

    assert not decision.fast_path
    assert decision.family == "link_state_verification"


@pytest.mark.parametrize("semantic_family", ["acl", "static_route"])
def test_direct_semantic_observation_precedes_downstream_packet_loss(semantic_family):
    result = diagnosis_result(
        {
            "verdict": "inconclusive",
            "confidence": 0.0,
            "reasoning": "The base diagnosis did not complete.",
        }
    )
    category = "configured_static_route" if semantic_family == "static_route" else "configuration_difference"
    value = (
        {"prefix": "203.0.113.0/24", "next_hop": "192.0.2.1", "protocol": "static"}
        if semantic_family == "static_route"
        else {"action": "drop", "status": "active"}
    )
    store = EvidenceStore(
        [
            Evidence(
                evidence_id="direct-semantic",
                entity_type="device",
                entity_id="leaf5",
                category=category,
                value=value,
                source="get_device_config",
                timestamp=datetime.now(UTC),
                origin=EvidenceOrigin.CONFIG_READ,
                metadata={"semantic_family": semantic_family},
            ),
            Evidence(
                evidence_id="downstream-loss",
                entity_type="path",
                entity_id="client1--client5",
                category="packet_loss_rate",
                value=0.25,
                source="pingmesh_episode",
                timestamp=datetime.now(UTC),
            ),
        ]
    )

    decision = HardCaseRouter().route(result=result, evidence_store=store)

    assert decision.family == semantic_family


@pytest.mark.parametrize(
    "text",
    [
        "No anomalies were observed and the network is healthy.",
        "BGP config is clean with no static routes or route-map filters.",
        "Routes are accepted and there is no inbound route-map.",
        "The network statement is inaccessible because the connected interface is down.",
    ],
)
def test_negative_or_downstream_policy_language_is_not_direct_policy_evidence(text):
    assert not has_route_policy_evidence(text)


def test_direct_link_down_outranks_downstream_route_consequence():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "link_down",
            "device": "leaf7",
            "interface": "Ethernet20",
            "confidence": 0.95,
            "evidence": [
                "get_device_interfaces leaf7: Ethernet20 is oper=down, admin=down.",
                "The connected route is absent, so the BGP network statement has no best path.",
            ],
        }
    )
    hints = extract_semantic_family_hints(result)
    store = EvidenceStore(evidence_from_diagnosis(result, hints))

    decision = HardCaseRouter().route(
        result=result,
        evidence_store=store,
        semantic_family_hints=hints,
    )

    assert decision.fast_path
    assert decision.family == "link_state_verification"


def test_large_link_down_prose_becomes_typed_state_and_not_route_policy():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "link_down",
            "device": "leaf7",
            "interface": "Ethernet20",
            "confidence": 0.95,
            "evidence": [
                "Ethernet20 on leaf7 is admin-down and oper-down for the client subnet.",
                "Route table on leaf7 has no connected route because the interface is down.",
            ],
            "reasoning": (
                "Without the connected route, the BGP network statement cannot install a valid path "
                "and the prefix is not advertised."
            ),
        }
    )
    hints = extract_semantic_family_hints(result)
    evidence = evidence_from_diagnosis(result, hints)
    store = EvidenceStore(evidence)

    assert not any(hint.family == "route_policy" for hint in hints)
    assert {item.category for item in evidence} >= {"interface_admin_state", "interface_oper_state"}
    decision = HardCaseRouter().route(
        result=result,
        evidence_store=store,
        semantic_family_hints=hints,
    )
    # Parsed prose remains useful for routing, but it is a claim rather than
    # independent live state and therefore cannot enter the fast path.
    assert not decision.fast_path
    assert decision.family == "link_state_verification"
    assert "missing_required_direct_evidence" in decision.reasons
    assert all(not item.supports_submission for item in evidence)


def test_bgp_network_statement_missing_prefix_is_specific_route_policy_evidence():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf5",
            "confidence": 0.98,
            "evidence": ["BGP config on leaf5 network statements include 192.168.105.0/30; MISSING 192.168.105.4/30."],
        }
    )
    hints = extract_semantic_family_hints(result)
    store = EvidenceStore(evidence_from_diagnosis(result, hints))

    assert [(hint.family, hint.prefix) for hint in hints] == [("route_policy", "192.168.105.4/30")]
    decision = HardCaseRouter().route(
        result=result,
        evidence_store=store,
        semantic_family_hints=hints,
    )
    assert decision.fast_path
    assert decision.family == "route_policy"


@pytest.mark.parametrize(
    ("evidence", "expected_prefix"),
    [
        (
            "leaf11 BGP config advertises network 192.168.111.4/30 and "
            "192.168.111.8/30 but omits network 192.168.111.0/30.",
            "192.168.111.0/30",
        ),
        (
            "The leaf11 address-family fails to advertise network 192.168.111.0/30.",
            "192.168.111.0/30",
        ),
        (
            "BGP configuration does not include network 192.168.111.0/30.",
            "192.168.111.0/30",
        ),
    ],
)
def test_bgp_network_omission_language_is_specific_route_policy_evidence(evidence, expected_prefix):
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf11",
            "confidence": 0.90,
            "evidence": [evidence],
        }
    )

    hints = extract_semantic_family_hints(result)
    decision = HardCaseRouter().route(
        result=result,
        evidence_store=_structured("packet_loss_rate"),
        semantic_family_hints=hints,
    )

    assert [(hint.family, hint.prefix) for hint in hints] == [("route_policy", expected_prefix)]
    assert decision.family == "route_policy"
    assert "semantic_label_conflicts_with_structured_impairment" not in decision.reasons


@pytest.mark.parametrize(
    "text",
    [
        "The route to 192.168.111.0/30 is omitted from the RIB.",
        "Traceroute omits intermediate hops before timing out.",
        "BGP configuration has no route-map filters and all expected networks are present.",
    ],
)
def test_generic_omission_language_is_not_route_policy_configuration_evidence(text):
    assert not has_route_policy_evidence(text)


def test_negated_interface_down_text_does_not_become_state_evidence():
    result = diagnosis_result(
        {
            "verdict": "network_healthy",
            "confidence": 0.95,
            "evidence": ["leaf7 Ethernet20 is not admin-down and not oper-down."],
        }
    )
    evidence = evidence_from_diagnosis(result)
    assert not any(item.category in {"interface_admin_state", "interface_oper_state"} for item in evidence)


def test_missing_connected_interface_routes_policy_label_to_link_state_verification():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf2",
            "interface": None,
            "confidence": 0.95,
            "evidence": [
                "192.168.102.8/30 is the subnet for which leaf2 has no connected interface.",
                "The configured BGP network statement cannot activate without the connected route.",
            ],
        }
    )

    decision = HardCaseRouter().route(result=result)

    assert not decision.fast_path
    assert decision.family == "link_state_verification"


def test_canonical_link_flapping_is_not_overwritten_by_negative_route_map_text():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "link_flapping",
            "device": "leaf11",
            "interface": "Ethernet12",
            "confidence": 0.95,
            "evidence": [
                "BGP session flap ESTABLISHED→ACTIVE→ESTABLISHED was observed on leaf11.",
                "leaf11 device logs show Ethernet12 went down twice during the window.",
                "BGP config is clean with no static routes or route-map filters.",
            ],
        }
    )
    hints = extract_semantic_family_hints(result)

    decision = HardCaseRouter().route(result=result, semantic_family_hints=hints)

    assert decision.fast_path
    assert decision.family == "link_flapping"
    assert {hint.family for hint in hints} == {"link_flapping"}


def test_healthy_with_structured_performance_symptom_escalates():
    healthy = diagnosis_result(
        {
            "verdict": "network_healthy",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.95,
            "evidence": ["Routes and interfaces are up."],
        }
    )

    decision = HardCaseRouter().route(result=healthy, evidence_store=_structured("packet_loss_rate"))

    assert not decision.fast_path
    assert decision.family == "packet_loss"
    assert "healthy_verdict_with_performance_symptom" in decision.reasons


def test_route_policy_conflict_is_escalated_without_relabeling():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "bgp_neighbor_misconfig",
            "device": "leaf1",
            "interface": None,
            "confidence": 0.92,
            "evidence": ["All BGP sessions are Established; the route-map network statement is missing."],
        }
    )

    decision = HardCaseRouter().route(result=result)

    assert result.findings["fault_type"] == "bgp_neighbor_misconfig"
    assert decision.family == "route_policy"
    assert "bgp_label_conflicts_with_route_policy_evidence" in decision.reasons


def test_speculative_route_policy_label_yields_to_structured_loss_branch():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf1",
            "interface": None,
            "confidence": 0.85,
            "evidence": ["BGP is established, but traffic through leaf1 has persistent packet loss."],
        }
    )

    decision = HardCaseRouter().route(result=result, evidence_store=_structured("packet_loss_rate"))

    assert not decision.fast_path
    assert decision.family == "packet_loss"
    assert "semantic_label_conflicts_with_structured_impairment" in decision.reasons


def test_direct_route_policy_configuration_evidence_keeps_semantic_label():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf1",
            "interface": None,
            "confidence": 0.95,
            "evidence": ["The route-map network statement is missing from the running configuration."],
        }
    )

    decision = HardCaseRouter().route(result=result, evidence_store=_structured("packet_loss_rate"))

    assert decision.fast_path
    assert decision.family == "route_policy"

    result.findings["evidence"] = [
        "The device only advertises one client prefix and has no network statement for the affected /30."
    ]
    decision = HardCaseRouter().route(result=result, evidence_store=_structured("packet_loss_rate"))
    assert decision.fast_path
    assert decision.family == "route_policy"

    result.findings["evidence"] = [
        "BGP config advertises network 192.168.105.4/30; network "
        "192.168.105.0/30 is missing from address-family ipv4 unicast.",
        "The BGP RIB confirms the affected prefix is not advertised.",
    ]
    decision = HardCaseRouter().route(result=result, evidence_store=_structured("packet_loss_rate"))
    assert decision.fast_path
    assert decision.family == "route_policy"


def test_base_recursion_with_static_route_symptom_uses_semantic_branch():
    result = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.0,
            "reasoning": "Recursion limit reached after finding a static route with an unresolvable next hop.",
        }
    )

    decision = HardCaseRouter().route(result=result)

    assert not decision.fast_path
    assert decision.family == "static_route"
    assert "base_runtime_failure" in decision.reasons


def test_base_recursion_without_fault_symptom_uses_healthy_verification():
    result = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.0,
            "reasoning": "GRAPH_RECURSION_LIMIT reached before a result was emitted.",
        }
    )

    decision = HardCaseRouter().route(result=result)

    assert not decision.fast_path
    assert decision.family == "healthy_verification"
    assert "base_runtime_failure" in decision.reasons


def test_tool_error_does_not_create_a_weak_fault_symptom():
    healthy = diagnosis_result(
        {
            "verdict": "network_healthy",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.95,
            "evidence": ["All bounded observations are healthy."],
        }
    )

    decision = HardCaseRouter().route(result=healthy, evidence_store=_structured("tool_error"))

    assert decision.fast_path


def test_unknown_label_cannot_take_fast_path_without_normalizer_metadata():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "mysterious_fault",
            "device": "leaf1",
            "interface": None,
            "confidence": 0.95,
            "evidence": ["A direct observation exists but the taxonomy label is unknown."],
        }
    )

    decision = HardCaseRouter().route(result=result)

    assert not decision.fast_path
    assert "noncanonical_fault_type" in decision.reasons


def test_high_confidence_route_policy_without_direct_route_evidence_is_vetoed():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf1",
            "interface": None,
            "confidence": 0.99,
            "evidence": ["Traffic is unreachable and route policy might be involved."],
        }
    )

    decision = HardCaseRouter().route(result=result)

    assert not decision.fast_path
    assert "missing_required_direct_evidence" in decision.reasons


def test_route_policy_with_interface_down_evidence_is_vetoed_to_link_state():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf1",
            "interface": None,
            "confidence": 0.99,
            "evidence": ["The network statement is missing from the running configuration."],
        }
    )
    store = EvidenceStore(
        [
            Evidence(
                evidence_id="E-down",
                entity_type="interface",
                entity_id="leaf1:Ethernet0",
                category="interface_oper_state",
                value="down",
                source="get_device_interfaces",
                timestamp=datetime.now(UTC),
            )
        ]
    )

    decision = HardCaseRouter().route(
        result=result,
        evidence_store=store,
    )

    assert not decision.fast_path
    assert decision.family == "link_state_verification"


def test_route_policy_with_selected_blackhole_route_is_vetoed_to_semantic_closure():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf2",
            "interface": None,
            "confidence": 0.95,
            "evidence": [
                "The route table confirms a selected static route ip route 192.0.2.0/24 Null0, "
                "which blackholes the affected traffic.",
                "A network statement also appears absent.",
            ],
        }
    )

    decision = HardCaseRouter().route(result=result)

    assert not decision.fast_path
    assert decision.family == "static_route"
    assert "semantic_conflict" in decision.reasons
    assert "semantic_conflict" in decision.reasons


@pytest.mark.parametrize(
    ("category", "value", "expected"),
    [
        ("syslog_event", {"temporal_transition": True}, "temporal_verification"),
        ("packet_loss_rate", 0.20, "packet_loss"),
        ("packet_size_threshold", {"size_dependent_failure": True}, "mtu"),
    ],
)
def test_recursion_routes_from_structured_public_evidence(category, value, expected):
    result = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.0,
            "reasoning": "Graph recursion limit reached.",
        }
    )
    store = EvidenceStore(
        [
            Evidence(
                evidence_id="E-public",
                entity_type="path",
                entity_id="client1--client2",
                category=category,
                value=value,
                source="public-observation",
                timestamp=datetime.now(UTC),
            )
        ]
    )

    assert HardCaseRouter().route(result=result, evidence_store=store).family == expected


def test_recursion_with_low_transient_loss_uses_temporal_verification():
    result = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.0,
            "reasoning": "Graph recursion limit reached.",
        }
    )

    low = EvidenceStore(
        [
            Evidence(
                evidence_id="E-low",
                entity_type="path",
                entity_id="client1--client2",
                category="packet_loss_rate",
                value=0.06,
                source="public-observation",
                timestamp=datetime.now(UTC),
            )
        ]
    )
    decision = HardCaseRouter().route(result=result, evidence_store=low)

    assert decision.family == "temporal_verification"


def test_recursion_with_early_only_loss_uses_temporal_verification():
    result = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.0,
            "reasoning": "GRAPH_RECURSION_LIMIT reached before a result was emitted.",
        }
    )
    store = EvidenceStore(
        [
            Evidence(
                evidence_id="E-early-loss",
                entity_type="path",
                entity_id="client1--client2",
                category="packet_loss_rate",
                value=0.15,
                source="pingmesh_episode",
                timestamp=datetime.now(UTC),
                reliability=0.8,
                metadata={"persistence": "early_only", "src_leaf": "leaf1", "dst_leaf": "leaf2"},
            )
        ]
    )

    decision = HardCaseRouter().route(result=result, evidence_store=store)

    assert not decision.fast_path
    assert decision.family == "temporal_verification"


def test_single_early_only_row_does_not_override_persistent_packet_loss():
    result = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "confidence": 0.0,
            "reasoning": "GRAPH_RECURSION_LIMIT reached before a result was emitted.",
        }
    )
    evidence = []
    for index in range(10):
        evidence.append(
            Evidence(
                evidence_id=f"E-loss-{index}",
                entity_type="path",
                entity_id=f"client{index}--client20",
                category="packet_loss_rate",
                value=0.15,
                source="pingmesh_episode",
                timestamp=datetime.now(UTC),
                reliability=0.8,
                metadata={"persistence": "early_only" if index == 0 else "full_window"},
            )
        )

    decision = HardCaseRouter().route(result=result, evidence_store=EvidenceStore(evidence))

    assert decision.family == "packet_loss"


def test_temporal_evidence_outvotes_packet_size_evidence_regardless_of_order():
    result = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "confidence": 0.0,
            "reasoning": "Graph recursion limit reached.",
        }
    )
    store = EvidenceStore(
        [
            Evidence(
                "mtu",
                "path",
                "client1--client2",
                "packet_size_threshold",
                {"size_dependent_failure": True},
                "pingmesh",
                datetime.now(UTC),
                reliability=0.7,
            ),
            Evidence(
                "flap",
                "interface",
                "leaf1:Ethernet0",
                "syslog_event",
                {"temporal_transition": True},
                "get_device_logs",
                datetime.now(UTC),
                reliability=1.0,
            ),
        ]
    )
    assert HardCaseRouter().route(result=result, evidence_store=store).family == "temporal_verification"


def test_missing_connected_route_vetoes_blackhole_fast_path_to_link_state():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "blackhole_route",
            "device": "leaf1",
            "confidence": 0.95,
            "evidence": ["leaf1 network 192.168.1.0/30 has no connected route and is not installed in the RIB."],
        }
    )
    decision = HardCaseRouter().route(result=result)
    assert not decision.fast_path
    assert decision.family == "link_state_verification"


def test_blackhole_label_without_selected_route_is_not_fast_path():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "blackhole_route",
            "device": "leaf1",
            "confidence": 0.96,
            "evidence": ["The observed loss is consistent with a blackhole route."],
        }
    )
    decision = HardCaseRouter().route(result=result)
    assert not decision.fast_path
    assert "missing_required_direct_evidence" in decision.reasons


def test_route_policy_missing_network_prefix_beats_loss_rows():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "packet_loss",
            "device": "leaf11",
            "confidence": 0.88,
            "evidence": ["leaf11 is missing the 'network 192.168.120.0/30' statement from the BGP address-family."],
        }
    )
    store = _structured("packet_loss_rate")
    decision = HardCaseRouter().route(
        result=result,
        evidence_store=store,
        semantic_family_hints=extract_semantic_family_hints(result),
    )
    assert decision.family == "route_policy"
    assert not decision.fast_path
    assert "specific_semantic_family_mismatch" in decision.reasons


def test_unreliable_unknown_weak_result_uses_bounded_generic_verification():
    result = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.2,
            "evidence": [],
            "reasoning": "A weak and unclassified symptom remains.",
        }
    )

    assert HardCaseRouter().route(result=result).family == "generic_verification"


def test_conflicts_and_close_hypotheses_escalate():
    store = _structured("packet_loss_rate")
    second = Evidence(
        evidence_id="E2",
        entity_type="path",
        entity_id="client1--client2",
        category="latency_p95",
        value=50.0,
        source="control",
        timestamp=datetime.now(UTC),
    )
    store.add(second)
    store.mark_conflict("E1", "E2")
    state = DiagnosticState(
        hypotheses={
            "H1": Hypothesis("H1", "packet_loss", None, None, None, probability=0.52),
            "H2": Hypothesis("H2", "high_latency", None, None, None, probability=0.48),
        }
    )
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "device_down",
            "device": "leaf1",
            "interface": None,
            "confidence": 0.95,
            "evidence": ["Device reachability is inconsistent."],
        }
    )

    decision = HardCaseRouter().route(result=result, state=state, evidence_store=store)

    assert not decision.fast_path
    assert "conflicting_evidence" in decision.reasons
    assert "close_hypothesis_scores" in decision.reasons


def test_link_down_without_interface_uses_link_state_before_impairment_routing():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "link_down",
            "device": "leaf1",
            "interface": None,
            "confidence": 0.95,
            "evidence": ["Device leaf1 is unreachable and paths through it fail."],
        }
    )
    store = EvidenceStore(
        [
            Evidence(
                evidence_id="E-loss",
                entity_type="path",
                entity_id="client1--client9",
                category="packet_loss_rate",
                value=1.0,
                source="public-observation",
                timestamp=datetime.now(UTC),
            )
        ]
    )

    decision = HardCaseRouter().route(result=result, evidence_store=store)

    assert decision.fast_path is False
    assert decision.family == "link_state_verification"
    assert "missing_required_interface" in decision.reasons


def test_unreliable_operational_claim_is_a_verification_hint_not_submission_evidence():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "device_down",
            "device": "spine4",
            "interface": None,
            "confidence": 0.95,
            "evidence": ["spine4 may be unavailable."],
        }
    )
    store = EvidenceStore(
        [
            Evidence(
                evidence_id="E-loss",
                entity_type="path",
                entity_id="client1--client9",
                category="packet_loss_rate",
                value=0.25,
                source="pingmesh_episode",
                timestamp=datetime.now(UTC),
            )
        ]
    )

    decision = HardCaseRouter().route(
        result=result,
        evidence_store=store,
        base_assessment=BaseAgentAssessment(
            BaseAgentStatus.LOW_EVIDENCE,
            ("missing_required_direct_evidence",),
        ),
    )

    assert not decision.fast_path
    assert decision.family == "link_state_verification"


def test_concentrated_many_peer_outage_overrides_packet_loss_label_for_operational_triage():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "packet_loss",
            "device": "leaf9",
            "interface": "Ethernet0",
            "confidence": 0.95,
            "evidence": ["Repeated loss is visible from one endpoint."],
        }
    )
    rows = []
    for index, peer in enumerate(("leaf1", "leaf2", "leaf3"), start=1):
        rows.append(
            Evidence(
                evidence_id=f"E-outage-{index}",
                entity_type="path",
                entity_id=f"client9--client{index}",
                category="packet_loss_rate",
                value=1.0,
                source="pingmesh_episode",
                timestamp=datetime.now(UTC),
                metadata={"src_leaf": "leaf9", "dst_leaf": peer},
            )
        )

    decision = HardCaseRouter().route(result=result, evidence_store=EvidenceStore(rows))

    assert decision.fast_path is False
    assert decision.family == "link_state_verification"
    assert "concentrated_outage_requires_device_scope_triage" in decision.reasons


def test_concentrated_outage_vetoes_link_down_fast_path_for_device_scope_triage():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "link_down",
            "device": "leaf9",
            "interface": "Ethernet0",
            "confidence": 0.95,
            "evidence": ["One interface is down."],
        }
    )
    rows = [
        Evidence(
            evidence_id=f"E-device-{index}",
            entity_type="path",
            entity_id=f"client9--client{index}",
            category="packet_loss_rate",
            value=1.0,
            source="pingmesh_episode",
            timestamp=datetime.now(UTC),
            metadata={"src_leaf": "leaf9", "dst_leaf": peer},
        )
        for index, peer in enumerate(("leaf1", "leaf2", "leaf3"), start=1)
    ]

    decision = HardCaseRouter().route(result=result, evidence_store=EvidenceStore(rows))

    assert not decision.fast_path
    assert decision.family == "link_state_verification"
    assert "concentrated_outage_requires_device_scope_triage" in decision.reasons


def test_healthy_base_prose_does_not_override_structured_healthy_observations():
    result = diagnosis_result(
        {
            "verdict": "network_healthy",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.95,
            "evidence": ["P99 latency was elevated to 7.5 ms but within the healthy range."],
        }
    )
    healthy = Evidence(
        evidence_id="E-healthy-rtt",
        entity_type="path",
        entity_id="client1--client2",
        category="latency_p95",
        value=7.5,
        source="pingmesh_episode",
        timestamp=datetime.now(UTC),
        metadata={"category_anomaly": False},
    )

    decision = HardCaseRouter().route(result=result, evidence_store=EvidenceStore([healthy]))

    assert decision.family == "healthy_verification"
    assert "healthy_verdict_with_performance_symptom" not in decision.reasons


def test_zero_loss_rows_are_healthy_observations_not_temporal_faults():
    result = diagnosis_result(
        {
            "verdict": "network_healthy",
            "confidence": 0.8,
            "evidence": ["No anomaly was found in the available network observations."],
        }
    )
    rows = [
        Evidence(
            evidence_id=f"zero-loss-{index}",
            entity_type="path",
            entity_id=f"leaf{index}--leaf16",
            category="packet_loss_rate",
            value=0.0,
            source="get_pingmesh_hotspots",
            timestamp=datetime.now(UTC),
            independence_key="tool:get_pingmesh_hotspots:one-call",
        )
        for index in range(16)
    ]

    decision = HardCaseRouter().route(result=result, evidence_store=EvidenceStore(rows))

    assert decision.family == "healthy_verification"


def test_negated_acl_prose_cannot_override_structured_packet_loss():
    result = diagnosis_result(
        {
            "verdict": "network_healthy",
            "confidence": 0.9,
            "evidence": ["No ACL blocks traffic; repeated telemetry instead shows packet loss."],
        }
    )
    loss = Evidence(
        evidence_id="E-loss",
        entity_type="path",
        entity_id="client1--client2",
        category="packet_loss_rate",
        value=0.2,
        source="pingmesh_episode",
        timestamp=datetime.now(UTC),
    )

    decision = HardCaseRouter().route(result=result, evidence_store=EvidenceStore([loss]))

    assert decision.family == "packet_loss"


def test_one_tool_response_cannot_outvote_independent_temporal_evidence_by_row_count():
    result = diagnosis_result(
        {
            "verdict": "inconclusive",
            "confidence": 0.0,
            "reasoning": "Diagnosis did not complete.",
        }
    )
    evidence = [
        Evidence(
            evidence_id=f"loss-{index}",
            entity_type="path",
            entity_id=f"leaf{index}--leaf16",
            category="packet_loss_rate",
            value=0.2,
            source="get_pingmesh_hotspots",
            timestamp=datetime.now(UTC),
            independence_key="tool:get_pingmesh_hotspots:one-call",
        )
        for index in range(64)
    ]
    evidence.append(
        Evidence(
            evidence_id="flap-event",
            entity_type="interface",
            entity_id="leaf9:Ethernet16",
            category="syslog_event",
            value={"temporal_transition": True},
            source="get_device_logs",
            timestamp=datetime.now(UTC),
            independence_key="event:leaf9:Ethernet16:1",
        )
    )

    decision = HardCaseRouter().route(result=result, evidence_store=EvidenceStore(evidence))

    assert decision.family == "temporal_verification"


def test_live_bgp_fault_routes_to_bgp_verification():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "bgp_neighbor_misconfig",
            "device": "leaf15",
            "confidence": 0.95,
            "evidence": ["leaf15 neighbor remote-AS mismatches and reports Bad Peer AS."],
        }
    )
    bgp = Evidence(
        evidence_id="E-bgp",
        entity_type="device",
        entity_id="leaf15",
        category="bgp_neighbor_state",
        value={"healthy": False, "neighbors": [{"state": "Idle"}]},
        source="get_bgp_neighbors",
        timestamp=datetime.now(UTC),
        metadata={"semantic_family": "bgp", "direct_bgp_evidence": True},
    )

    decision = HardCaseRouter().route(result=result, evidence_store=EvidenceStore([bgp]))

    assert decision.family == "bgp_verification"
    assert decision.fast_path


def test_unreliable_base_routes_by_direct_live_bgp_state():
    result = diagnosis_result(
        {
            "verdict": "inconclusive",
            "confidence": 0.0,
            "reasoning": "Graph recursion limit reached.",
        }
    )
    bgp = Evidence(
        evidence_id="E-bgp",
        entity_type="device",
        entity_id="leaf15",
        category="bgp_neighbor_state",
        value={"healthy": False, "neighbors": [{"state": "Idle"}]},
        source="get_bgp_neighbors",
        timestamp=datetime.now(UTC),
        metadata={"direct_bgp_evidence": True},
    )

    decision = HardCaseRouter().route(result=result, evidence_store=EvidenceStore([bgp]))

    assert decision.family == "bgp_verification"


def test_direct_bgp_configuration_error_outvotes_wrong_provider_family():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf15",
            "confidence": 0.92,
            "evidence": ["A route-policy issue may explain the missing path."],
        }
    )
    bgp = Evidence(
        evidence_id="E-bgp-config",
        entity_type="device",
        entity_id="leaf15",
        category="bgp_neighbor_state",
        value={"healthy": False, "neighbors": [{"state": "Idle", "last_error": "Bad Peer AS"}]},
        source="base_tool:get_bgp_neighbors",
        timestamp=datetime.now(UTC),
        origin=EvidenceOrigin.LIVE_TELEMETRY,
        metadata={"direct_bgp_evidence": True, "direct_bgp_configuration_evidence": True},
    )

    decision = HardCaseRouter().route(result=result, evidence_store=EvidenceStore([bgp]))

    assert decision.family == "bgp_verification"
    assert "direct_bgp_configuration_overrides_base_family" in decision.reasons


def test_exact_directional_latency_outvotes_wrong_link_down_label():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "link_down",
            "device": "leaf2",
            "interface": "Ethernet0",
            "confidence": 0.90,
            "evidence": ["The link may be down."],
        }
    )
    latency = Evidence(
        evidence_id="E-latency-link",
        entity_type="path",
        entity_id="leaf2--spine1",
        category="latency_median",
        value=120.0,
        source="base_tool:latency_link_test",
        timestamp=datetime.now(UTC),
        origin=EvidenceOrigin.LIVE_TELEMETRY,
        covered_links=("spine1:Ethernet4--leaf2:Ethernet0",),
        observed_path=("spine1:Ethernet4--leaf2:Ethernet0",),
        path_observation_confidence=1.0,
        metadata={"category_anomaly": True, "absolute_anomaly": True},
    )

    decision = HardCaseRouter().route(result=result, evidence_store=EvidenceStore([latency]))

    assert decision.family == "high_latency"
    assert any(reason.startswith("direct_impairment_observation_") for reason in decision.reasons)


def test_concrete_mtu_comparison_is_a_bounded_verification_hint():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "mtu_mismatch",
            "device": "spine2",
            "interface": "Ethernet12",
            "confidence": 0.75,
            "evidence": ["Ethernet12 MTU is 1400 while its peer MTU is 9100."],
        }
    )

    decision = HardCaseRouter().route(result=result, evidence_store=EvidenceStore())

    assert not decision.fast_path
    assert decision.family == "mtu"


def test_negated_flap_prose_does_not_create_temporal_branch():
    result = diagnosis_result(
        {
            "verdict": "network_healthy",
            "confidence": 0.95,
            "evidence": ["All links are up and there are no flap events."],
        }
    )

    decision = HardCaseRouter().route(result=result, evidence_store=EvidenceStore())

    assert decision.family != "temporal_verification"


def test_repeated_live_transitions_outvote_wrong_link_down_label():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "link_down",
            "device": "leaf1",
            "interface": "Ethernet4",
            "confidence": 0.90,
            "evidence": ["The interface is currently reachable."],
        }
    )
    events = [
        Evidence(
            evidence_id=f"E-flap-{index}",
            entity_type="interface",
            entity_id="leaf1:Ethernet4",
            category="syslog_event",
            value={
                "temporal_transition": True,
                "message": message,
            },
            source="get_device_logs",
            timestamp=datetime.now(UTC),
            origin=EvidenceOrigin.LIVE_TELEMETRY,
            independence_key=f"event-{index}",
            metadata={"link_id": "leaf1:Ethernet4--spine1:Ethernet0"},
        )
        for index, message in enumerate(
            (
                "Port Ethernet4 oper state set from up to down",
                "Port Ethernet4 oper state set from down to up",
            )
        )
    ]

    decision = HardCaseRouter().route(result=result, evidence_store=EvidenceStore(events))

    assert decision.family == "temporal_verification"


def test_persistent_link_down_error_logs_do_not_become_a_flap_cycle():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "link_down",
            "device": "leaf2",
            "interface": "Ethernet12",
            "confidence": 0.95,
            "evidence": ["The interface is administratively and operationally down."],
        }
    )
    evidence = [
        Evidence(
            evidence_id="down-state",
            entity_type="interface",
            entity_id="leaf2:Ethernet12",
            category="interface_oper_state",
            value="down",
            source="get_device_interfaces",
            timestamp=datetime.now(UTC),
            origin=EvidenceOrigin.LIVE_TELEMETRY,
        ),
        *[
            Evidence(
                evidence_id=f"error-{index}",
                entity_type="interface",
                entity_id="leaf2:Ethernet12",
                category="syslog_event",
                value={"temporal_transition": True, "message": message},
                source="get_device_logs",
                timestamp=datetime.now(UTC),
                origin=EvidenceOrigin.LIVE_TELEMETRY,
                independence_key=f"event-{index}",
                metadata={"link_id": "leaf2:Ethernet12--client4:eth1"},
            )
            for index, message in enumerate(
                (
                    "Port Ethernet12 oper error event: no_rx_reachability occurred",
                    "Port Ethernet12 oper error event: crc_rate occurred",
                )
            )
        ],
    ]

    decision = HardCaseRouter().route(result=result, evidence_store=EvidenceStore(evidence))

    assert decision.family == "link_state_verification"


def test_direct_public_operational_evidence_is_provider_result_invariant():
    down = Evidence(
        evidence_id="E-direct-down",
        entity_type="interface",
        entity_id="leaf5:Ethernet4",
        category="interface_oper_state",
        value="down",
        source="get_device_interfaces",
        timestamp=datetime.now(UTC),
    )
    variants = [
        diagnosis_result(
            {
                "verdict": "fault_detected",
                "fault_type": "route_policy_misconfig",
                "device": "leaf1",
                "confidence": 0.95,
                "evidence": ["A route policy may be involved."],
            }
        ),
        diagnosis_result(
            {
                "verdict": "network_healthy",
                "fault_type": None,
                "device": None,
                "confidence": 0.9,
                "evidence": ["No issue found."],
            }
        ),
        diagnosis_result(
            {
                "verdict": "inconclusive",
                "fault_type": None,
                "device": None,
                "confidence": 0.0,
                "reasoning": "Provider recursion limit reached.",
            }
        ),
    ]

    decisions = [HardCaseRouter().route(result=item, evidence_store=EvidenceStore([down])) for item in variants]

    assert {decision.family for decision in decisions} == {"link_state_verification"}
    assert not any(decision.fast_path for decision in decisions)


def test_partial_random_loss_does_not_trigger_device_scope_triage():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "packet_loss",
            "device": "leaf9",
            "interface": "Ethernet0",
            "confidence": 0.95,
            "evidence": ["Repeated packet loss was observed."],
        }
    )
    rows = [
        Evidence(
            evidence_id=f"E-loss-{index}",
            entity_type="path",
            entity_id=f"client9--client{index}",
            category="packet_loss_rate",
            value=0.20,
            source="repeated_probe",
            timestamp=datetime.now(UTC),
            metadata={"src_leaf": "leaf9", "dst_leaf": peer, "rounds": 2},
        )
        for index, peer in enumerate(("leaf1", "leaf2", "leaf3"), start=1)
    ]

    assert HardCaseRouter().route(result=result, evidence_store=EvidenceStore(rows)).family == "packet_loss"


def test_unreliable_many_source_partial_loss_uses_route_contrast_before_loss():
    result = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.0,
            "evidence": [],
            "reasoning": "Graph recursion limit reached.",
        }
    )
    rows = [
        Evidence(
            evidence_id=f"E-star-{index}",
            entity_type="path",
            entity_id=f"client{index}--client20",
            category="packet_loss_rate",
            value=0.25,
            source="pingmesh_episode",
            timestamp=datetime.now(UTC),
            metadata={
                "src_leaf": source_leaf,
                "dst_leaf": "leaf5",
                "src_ip": f"192.0.2.{index}",
                "dst_ip": "192.0.2.20",
            },
        )
        for index, source_leaf in enumerate(("leaf1", "leaf2", "leaf3"), start=1)
    ]

    decision = HardCaseRouter().route(result=result, evidence_store=EvidenceStore(rows))

    assert decision.family == "runtime_semantic"
    assert not decision.fast_path


def test_direct_structured_interface_down_overrides_unrelated_base_family():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf1",
            "confidence": 0.95,
            "evidence": ["A policy issue may explain the symptom."],
        }
    )
    store = EvidenceStore(
        [
            Evidence(
                evidence_id="E-oper-down",
                entity_type="interface",
                entity_id="leaf5:Ethernet4",
                category="interface_oper_state",
                value="down",
                source="base_tool:get_device_interfaces",
                timestamp=datetime.now(UTC),
            )
        ]
    )

    decision = HardCaseRouter().route(result=result, evidence_store=store)

    assert decision.fast_path is False
    assert decision.family == "link_state_verification"
    assert "direct_interface_state_overrides_base_family" in decision.reasons


def test_explicit_link_down_state_with_complete_location_uses_fast_path():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "link_down",
            "device": "spine3",
            "interface": "Ethernet12",
            "confidence": 0.95,
            "evidence": [
                "spine3:Ethernet12 oper=down admin=down",
                "The physical peer reports the same interface-down state.",
            ],
        }
    )

    decision = HardCaseRouter().route(result=result)

    assert decision.fast_path
    assert decision.family == "link_state_verification"


def test_observable_family_excludes_failed_semantic_claims_during_impairment_fallback():
    store = EvidenceStore(
        [
            Evidence(
                "E-policy-miss",
                "device",
                "leaf1",
                "route_presence",
                {"prefix": "192.0.2.0/24", "route_count": 0},
                "get_bgp_rib",
                datetime.now(UTC),
                metadata={"semantic_family": "route_policy"},
                supports_submission=False,
            ),
            Evidence(
                "E-mtu-suspect",
                "path",
                "client1--client2",
                "packet_size_threshold",
                {"size_dependent_failure": True, "threshold_payload_size": None},
                "pingmesh_episode",
                datetime.now(UTC),
                metadata={"planning_only": True},
                supports_submission=False,
            ),
        ]
    )

    assert HardCaseRouter().observable_family(store) == "mtu"


def test_observable_family_prefers_ordinary_loss_over_unverified_df_suspect():
    store = EvidenceStore(
        [
            Evidence(
                "E-mtu-suspect",
                "path",
                "client1--client2",
                "packet_size_threshold",
                {"size_dependent_failure": True, "threshold_payload_size": None},
                "pingmesh_episode",
                datetime.now(UTC),
                metadata={"planning_only": True},
                supports_submission=False,
                independence_key="episode-window",
            ),
            Evidence(
                "E-ordinary-loss",
                "path",
                "client3--client4",
                "packet_loss_rate",
                0.20,
                "pingmesh_episode",
                datetime.now(UTC),
                independence_key="episode-window",
            ),
        ]
    )

    assert HardCaseRouter().observable_family(store) == "packet_loss"


def test_schema_failed_base_uses_planning_only_df_signature_to_select_mtu_sweep():
    result = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.0,
            "reasoning": "DiagnosisOutput JSON block missing or invalid in runtime result",
        }
    )
    store = EvidenceStore(
        [
            Evidence(
                "E-mtu-suspect",
                "path",
                "client1--client2",
                "packet_size_threshold",
                {"size_dependent_failure": True, "threshold_payload_size": None},
                "pingmesh_episode",
                datetime.now(UTC),
                metadata={"planning_only": True},
                supports_submission=False,
                independence_key="episode-window",
            ),
            Evidence(
                "E-ordinary-loss",
                "path",
                "client1--client2",
                "packet_loss_rate",
                0.24,
                "pingmesh_episode",
                datetime.now(UTC),
                independence_key="episode-window",
            ),
        ]
    )

    decision = HardCaseRouter().route(result=result, evidence_store=store)

    assert not decision.fast_path
    assert decision.family == "mtu"
    assert "direct_impairment_observation_selected_family" in decision.reasons


def test_valid_base_is_not_overridden_by_planning_only_df_signature():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "packet_loss",
            "device": "leaf1",
            "interface": "Ethernet0",
            "confidence": 0.95,
            "evidence": ["Repeated active probes measured persistent 20% packet loss."],
        }
    )
    store = EvidenceStore(
        [
            Evidence(
                "E-mtu-planning-only",
                "path",
                "client1--client2",
                "packet_size_threshold",
                {"size_dependent_failure": True, "threshold_payload_size": None},
                "pingmesh_episode",
                datetime.now(UTC),
                metadata={"planning_only": True},
                supports_submission=False,
                independence_key="episode-window",
            ),
            Evidence(
                "E-direct-loss",
                "path",
                "client1--client2",
                "packet_loss_rate",
                0.20,
                "active_packet_probe",
                datetime.now(UTC),
                origin=EvidenceOrigin.ACTIVE_PROBE,
                independence_key="active-loss-rounds",
                metadata={"rounds": 3},
            ),
        ]
    )

    decision = HardCaseRouter().route(result=result, evidence_store=store)

    assert decision.family == "packet_loss"
    assert "direct_impairment_observation_overrides_base_family" not in decision.reasons


def test_single_bgp_down_transition_does_not_override_direct_configuration_fault():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "bgp_neighbor_misconfig",
            "device": "leaf9",
            "confidence": 0.95,
            "evidence": ["Bad Peer AS on leaf9 neighbor."],
        }
    )
    store = EvidenceStore(
        [
            Evidence(
                "E-bgp-config",
                "device",
                "leaf9",
                "bgp_neighbor_state",
                {"healthy": False},
                "get_bgp_neighbors",
                datetime.now(UTC),
                metadata={"direct_bgp_evidence": True, "direct_bgp_configuration_evidence": True},
            ),
            Evidence(
                "E-one-transition",
                "device",
                "leaf9",
                "bgp_neighbor_state",
                {"states_observed": ["ESTABLISHED", "IDLE"], "temporal_transition": True},
                "query_bgp_events",
                datetime.now(UTC),
            ),
        ]
    )
    assessment = BaseAgentAssessment(BaseAgentStatus.VALID, (), True, False)

    decision = HardCaseRouter().route(result=result, evidence_store=store, base_assessment=assessment)

    assert decision.family == "bgp_verification"
    assert "structured_temporal_transition_requires_verification" not in decision.reasons


def test_complete_bgp_state_cycle_still_routes_temporal_verification():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "bgp_neighbor_misconfig",
            "device": "leaf9",
            "confidence": 0.95,
            "evidence": ["Neighbor state changed during the window."],
        }
    )
    store = EvidenceStore(
        [
            Evidence(
                "E-cycle",
                "device",
                "leaf9",
                "bgp_neighbor_state",
                {"states_observed": ["ESTABLISHED", "IDLE", "ESTABLISHED"], "temporal_transition": True},
                "query_bgp_events",
                datetime.now(UTC),
            )
        ]
    )
    assessment = BaseAgentAssessment(BaseAgentStatus.VALID, (), True, False)

    decision = HardCaseRouter().route(result=result, evidence_store=store, base_assessment=assessment)

    assert decision.family == "temporal_verification"


def test_healthy_result_with_only_weak_aggregate_loss_uses_healthy_verification():
    result = diagnosis_result(
        {
            "verdict": "network_healthy",
            "fault_type": None,
            "device": None,
            "confidence": 0.8,
            "evidence": ["No material network fault was observed."],
        }
    )
    store = EvidenceStore(
        [
            Evidence(
                "E-weak-loss",
                "path",
                "aggregate",
                "packet_loss_rate",
                0.03,
                "pingmesh_episode",
                datetime.now(UTC),
                metadata={"aggregate_pingmesh": True, "weak_performance_symptom": True},
            )
        ]
    )
    assessment = BaseAgentAssessment(BaseAgentStatus.LOW_EVIDENCE, (), False, True)

    decision = HardCaseRouter().route(result=result, evidence_store=store, base_assessment=assessment)

    assert decision.family == "healthy_verification"
