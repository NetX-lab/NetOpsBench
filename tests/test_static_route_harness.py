from datetime import UTC, datetime

from examples.agents.diagnostic_harness.evidence.semantic import (
    evidence_from_diagnosis,
    extract_semantic_family_hints,
)
from examples.agents.diagnostic_harness.evidence.store import EvidenceStore
from examples.agents.diagnostic_harness.hypotheses.diagnosability import DiagnosabilityGate
from examples.agents.diagnostic_harness.models import (
    BaseAgentAssessment,
    BaseAgentStatus,
    Evidence,
    Hypothesis,
)
from examples.agents.diagnostic_harness.normalization.interface import TopologyIndex
from examples.agents.diagnostic_harness.routing.hard_case_router import HardCaseRouter

from .diagnostic_harness_helpers import diagnosis_result


def _static_result(**overrides):
    payload = {
        "verdict": "fault_detected",
        "fault_type": "static_route_misconfig",
        "device": "leaf8",
        "interface": None,
        "confidence": 0.95,
        "evidence": [
            "leaf8 config contains ip route 192.168.105.6/32 192.168.108.2, pointing to a non-gateway client",
            "leaf8 route table confirms the static route is selected via Ethernet16",
            "ping fails with ICMP redirect from the configured next-hop",
        ],
    }
    payload.update(overrides)
    return diagnosis_result(payload)


def test_low_evidence_static_route_keeps_specific_hint_and_routes_static_branch():
    result = _static_result()
    hints = extract_semantic_family_hints(result)
    assert hints and hints[0].family == "static_route"
    assert hints[0].prefix == "192.168.105.6/32"
    decision = HardCaseRouter().route(
        result=result,
        base_assessment=BaseAgentAssessment(BaseAgentStatus.LOW_EVIDENCE, ("missing_required_direct_evidence",)),
        semantic_family_hints=hints,
    )
    assert not decision.fast_path
    assert decision.family == "static_route"


def test_semantic_device_extraction_uses_inventory_not_role_name_patterns():
    topology = TopologyIndex(devices={"server-1": "client", "tor-west-a": "tor"})
    result = _static_result(
        device="tor-west-a",
        evidence=["tor-west-a config contains incorrect ip route 192.0.2.0/24 198.51.100.1"],
    )

    hints = extract_semantic_family_hints(result, topology)

    assert hints[0].device == "tor-west-a"


def test_negated_acl_observation_does_not_create_semantic_hint():
    result = diagnosis_result(
        {
            "verdict": "network_healthy",
            "confidence": 0.7,
            "evidence": ["All interfaces are up; no ACLs or iptables rules are blocking traffic."],
        }
    )

    assert not extract_semantic_family_hints(result)


def test_acl_subject_followed_by_no_blocking_rules_is_not_a_fault_hint():
    result = diagnosis_result(
        {
            "verdict": "network_healthy",
            "confidence": 0.7,
            "evidence": ["ACLs on leaf9 and leaf13 show no blocking rules."],
        }
    )

    assert not extract_semantic_family_hints(result)
    assert not evidence_from_diagnosis(result)


def test_packet_loss_does_not_overwrite_specific_static_hint():
    result = _static_result()
    hints = extract_semantic_family_hints(result)
    store = EvidenceStore(
        [
            Evidence("loss", "path", "a--b", "packet_loss_rate", 0.9, "pingmesh", datetime.now(UTC)),
        ]
    )
    decision = HardCaseRouter().route(
        result=result,
        evidence_store=store,
        base_assessment=BaseAgentAssessment(BaseAgentStatus.LOW_EVIDENCE),
        semantic_family_hints=hints,
    )
    assert decision.family == "static_route"


def test_recursion_without_route_information_has_no_static_hint():
    result = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "confidence": 0.0,
            "reasoning": "GRAPH_RECURSION_LIMIT reached.",
        }
    )
    assert not extract_semantic_family_hints(result)


def test_route_policy_is_specific_semantic_hint_not_static_route():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf1",
            "confidence": 0.95,
            "evidence": ["route-map policy denies the affected prefix"],
        }
    )
    hints = extract_semantic_family_hints(result)
    assert hints and hints[0].family == "route_policy"
    assert all(item.family != "static_route" for item in hints)


def test_link_flapping_text_is_retained_as_temporal_hint():
    result = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "confidence": 0.0,
            "reasoning": "The BGP session flapped and the interface went down/up during the window.",
        }
    )
    hints = extract_semantic_family_hints(result)
    assert hints and hints[0].family == "link_flapping"


def test_direct_link_flapping_text_becomes_typed_temporal_evidence():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "link_flapping",
            "device": "leaf2",
            "interface": "Ethernet4",
            "confidence": 0.95,
            "evidence": [
                "BGP session flap ESTABLISHED→ACTIVE→ESTABLISHED was observed on leaf2.",
                "leaf2 device logs show Ethernet4 went down during the episode window.",
            ],
        }
    )

    evidence = evidence_from_diagnosis(result, extract_semantic_family_hints(result))

    assert {item.category for item in evidence} == {"bgp_neighbor_state", "syslog_event"}
    assert all(item.metadata["direct_temporal_evidence"] for item in evidence)
    assert any(item.entity_id == "leaf2:Ethernet4" for item in evidence)


def test_static_route_text_becomes_structured_evidence():
    result = _static_result()
    evidence = evidence_from_diagnosis(result)
    categories = {item.category for item in evidence}
    assert {
        "configured_static_route",
        "unexpected_next_hop",
        "observed_routing_entry",
        "route_reachability_consequence",
    } <= categories
    assert all(
        item.metadata["direct_configuration_evidence"]
        for item in evidence
        if item.category == "configured_static_route"
    )


def test_static_route_sends_traffic_to_client_is_specific_hint():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "static_route_misconfig",
            "device": "leaf8",
            "confidence": 0.95,
            "evidence": [
                "leaf8 config: ip route 192.168.105.6/32 192.168.108.2 — static route sends traffic to client15."
            ],
        }
    )
    assert extract_semantic_family_hints(result)[0].family == "static_route"


def test_static_route_gate_does_not_require_interface_or_payload_integrity():
    evidence = [
        Evidence(
            "cfg", "device", "leaf8", "configured_static_route", {"prefix": "10.0.0.0/32"}, "config", datetime.now(UTC)
        ),
        Evidence(
            "diff", "device", "leaf8", "unexpected_next_hop", {"prefix": "10.0.0.0/32"}, "route", datetime.now(UTC)
        ),
        Evidence(
            "consequence", "path", "leaf8:10.0.0.0/32", "route_reachability_consequence", {}, "ping", datetime.now(UTC)
        ),
    ]
    h = Hypothesis(
        "H-static",
        "static_route_misconfig",
        "leaf8",
        None,
        None,
        score=3,
        probability=0.9,
        supporting_evidence=["cfg", "diff", "consequence"],
    )
    decision = DiagnosabilityGate().analyze({"H-static": h}, evidence, [])
    assert decision.can_submit


def test_null0_blackhole_hint_emits_discard_difference_evidence():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "static_route_misconfig",
            "device": "leaf3",
            "confidence": 0.95,
            "evidence": [
                "Leaf3 config contains 'ip route 192.168.108.0/30 Null0' — a static blackhole route.",
                "The route table shows the Null0 route selected and traffic is unreachable.",
            ],
        }
    )
    hints = extract_semantic_family_hints(result)
    evidence = evidence_from_diagnosis(result, hints)
    assert hints and hints[0].family == "static_route"
    assert any(item.category == "configured_static_route" for item in evidence)
    assert any(item.category == "unexpected_next_hop" for item in evidence)


def test_zero_packet_loss_does_not_escalate_healthy_result():
    result = diagnosis_result(
        {
            "verdict": "network_healthy",
            "confidence": 0.95,
            "evidence": ["Pingmesh reports 0% packet loss and normal paths."],
        }
    )
    store = EvidenceStore(
        [
            Evidence(
                "healthy-loss",
                "path",
                "client1--client2",
                "packet_loss_rate",
                0.0,
                "pingmesh",
                datetime.now(UTC),
            )
        ]
    )
    decision = HardCaseRouter().route(result=result, evidence_store=store)
    assert decision.fast_path
