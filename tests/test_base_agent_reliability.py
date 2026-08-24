from dataclasses import replace
from datetime import UTC, datetime

import pytest

from examples.agents.diagnostic_harness.evidence import EvidenceStore
from examples.agents.diagnostic_harness.models import BaseAgentStatus, Evidence
from examples.agents.diagnostic_harness.verification.base_reliability import BaseAgentReliability

from .diagnostic_harness_helpers import diagnosis_result


def _result(**overrides):
    payload = {
        "verdict": "fault_detected",
        "fault_type": "route_policy_misconfig",
        "device": "leaf1",
        "interface": None,
        "confidence": 0.95,
        "evidence": ["The network statement is missing from the running configuration."],
    }
    payload.update(overrides)
    return diagnosis_result(payload)


def _evidence(category, value, *, reliability=1.0):
    return Evidence(
        evidence_id=f"E-{category}",
        entity_type="interface",
        entity_id="leaf1:Ethernet0",
        category=category,
        value=value,
        source="public-tool",
        timestamp=datetime.now(UTC),
        reliability=reliability,
    )


@pytest.mark.parametrize("error_type", ["GraphRecursionError", "LangGraphRecursionError"])
def test_recursion_failure_has_typed_status(error_type):
    result = replace(
        _result(verdict="inconclusive", fault_type=None, confidence=0.0, evidence=[]),
        metadata={"error_type": error_type},
    )

    assessment = BaseAgentReliability().assess(result)

    assert assessment.status == BaseAgentStatus.RECURSION_FAILED
    assert not assessment.direct_evidence


def test_invalid_schema_and_zero_confidence_are_not_reliable():
    schema = BaseAgentReliability().assess(_result(), normalization_errors=("invalid verdict",))
    low = BaseAgentReliability().assess(_result(verdict="inconclusive", fault_type=None, confidence=0.0, evidence=[]))

    assert schema.status == BaseAgentStatus.SCHEMA_FAILED
    assert low.status == BaseAgentStatus.LOW_EVIDENCE


def test_repeated_failed_action_signature_is_tool_loop_status():
    result = replace(
        _result(verdict="inconclusive", fault_type=None, confidence=0.2, evidence=[]),
        metadata={
            "tool_calls": [
                {"tool": "get_interface_metrics", "args": {"device": "leaf1", "bad": True}},
                {"tool": "get_interface_metrics", "args": {"device": "leaf1", "bad": True}},
            ]
        },
    )

    assert BaseAgentReliability().assess(result).status == BaseAgentStatus.TOOL_LOOP_FAILED


@pytest.mark.parametrize(
    "observation",
    [
        "Traceroute reaches spine1 and then all remaining hops time out.",
        "The ICMP request timed out between client1 and client9.",
        "The remote TCP connection closed after the network fault was injected.",
    ],
)
def test_network_timeout_language_is_not_an_agent_runtime_failure(observation):
    result = _result(
        confidence=0.60,
        evidence=[observation, "The suspected device is leaf1."],
    )

    assessment = BaseAgentReliability().assess(result)

    assert assessment.status == BaseAgentStatus.LOW_EVIDENCE
    assert "provider_or_runtime_failure" not in assessment.reasons


@pytest.mark.parametrize(
    ("metadata", "expected_status"),
    [
        (
            {
                "error_type": "ProviderError",
                "agent_failure_stage": "provider",
                "base_exception_contained": True,
            },
            BaseAgentStatus.TOOL_LOOP_FAILED,
        ),
        (
            {
                "error_type": "MCPInfrastructureError",
                "agent_failure_stage": "tool_infrastructure",
                "base_exception_contained": True,
            },
            BaseAgentStatus.TOOL_INFRASTRUCTURE_FAILED,
        ),
        (
            {"error_type": "ProviderError"},
            BaseAgentStatus.TOOL_LOOP_FAILED,
        ),
    ],
)
def test_structured_agent_failure_metadata_controls_runtime_classification(metadata, expected_status):
    result = replace(
        _result(verdict="inconclusive", fault_type=None, confidence=0.0, evidence=[]),
        metadata=metadata,
    )

    assert BaseAgentReliability().assess(result).status == expected_status


def test_high_confidence_label_without_required_evidence_is_low_evidence():
    result = _result(evidence=["Traffic is unreachable, so policy may be involved."])

    assessment = BaseAgentReliability().assess(result)

    assert assessment.status == BaseAgentStatus.LOW_EVIDENCE
    assert "missing_required_direct_evidence" in assessment.reasons


def test_interface_down_conflicts_with_route_policy_result():
    store = EvidenceStore([_evidence("interface_oper_state", "down")])

    assessment = BaseAgentReliability().assess(_result(), evidence_store=store)

    assert assessment.status == BaseAgentStatus.LOW_EVIDENCE
    assert assessment.semantic_conflict


def test_healthy_interface_state_is_not_a_fault_conflict():
    store = EvidenceStore(
        [
            _evidence("interface_admin_state", "up"),
            replace(_evidence("interface_oper_state", "up"), evidence_id="E-oper-up"),
        ]
    )
    result = _result(
        verdict="network_healthy",
        fault_type=None,
        device=None,
        confidence=0.95,
        evidence=["Connectivity, routes, BGP, and interface observations are healthy."],
    )

    assessment = BaseAgentReliability().assess(result, evidence_store=store)

    assert not assessment.semantic_conflict


def test_live_abnormal_bgp_state_vetoes_healthy_fast_path():
    bgp = replace(
        _evidence("bgp_neighbor_state", {"healthy": False, "neighbors": [{"state": "Idle"}]}),
        metadata={"direct_bgp_evidence": True},
    )
    result = _result(
        verdict="network_healthy",
        fault_type=None,
        device=None,
        confidence=0.95,
        evidence=["Connectivity and interfaces appear healthy."],
    )

    assessment = BaseAgentReliability().assess(result, evidence_store=EvidenceStore([bgp]))

    assert assessment.status == BaseAgentStatus.LOW_EVIDENCE
    assert assessment.semantic_conflict


def test_selected_blackhole_route_conflicts_with_route_policy_result():
    result = _result(
        evidence=[
            "The route table confirms a selected static route ip route 192.0.2.0/24 Null0.",
            "A separate network statement appears absent.",
        ]
    )

    assessment = BaseAgentReliability().assess(result)

    assert assessment.status == BaseAgentStatus.LOW_EVIDENCE
    assert assessment.semantic_conflict


def test_device_crash_description_vetoes_link_down_fast_path():
    result = _result(
        fault_type="link_down",
        device="leaf4",
        interface="Ethernet4",
        evidence=["leaf4:Ethernet4 is unreachable because spine2 crashed and the entire device is offline."],
    )

    assessment = BaseAgentReliability().assess(result)

    assert assessment.status == BaseAgentStatus.LOW_EVIDENCE
    assert assessment.semantic_conflict


def test_tool_error_never_makes_a_base_result_reliable_or_supports_a_fault():
    store = EvidenceStore([_evidence("tool_error", {"error": "timeout"}, reliability=0.0)])

    assessment = BaseAgentReliability().assess(
        _result(evidence=["Traffic is unreachable, so policy may be involved."]),
        evidence_store=store,
    )

    assert assessment.status == BaseAgentStatus.LOW_EVIDENCE


def test_canonical_route_policy_with_direct_evidence_is_valid():
    assessment = BaseAgentReliability().assess(_result())

    assert assessment.status == BaseAgentStatus.VALID
    assert assessment.direct_evidence


def test_live_abnormal_bgp_state_is_direct_evidence():
    result = _result(
        fault_type="bgp_neighbor_misconfig",
        evidence=["The peer has a remote-AS mismatch and reports Bad Peer AS."],
    )
    evidence = _evidence(
        "bgp_neighbor_state",
        {"healthy": False, "neighbors": [{"state": "Idle", "neighbor": "10.0.0.1"}]},
    )
    evidence = replace(evidence, metadata={"semantic_family": "bgp", "direct_bgp_evidence": True})

    assessment = BaseAgentReliability().assess(result, evidence_store=EvidenceStore([evidence]))

    assert assessment.status == BaseAgentStatus.VALID
    assert assessment.direct_evidence


def test_route_policy_missing_prefix_advertisement_is_direct_evidence():
    result = _result(
        evidence=[
            "BGP config advertises network 192.168.105.4/30; network "
            "192.168.105.0/30 is missing from address-family ipv4 unicast.",
            "BGP RIB confirms the affected prefix is not advertised.",
        ]
    )

    assessment = BaseAgentReliability().assess(result)

    assert assessment.status == BaseAgentStatus.VALID
    assert assessment.direct_evidence


def test_selected_static_route_to_non_routing_endpoint_is_direct_evidence():
    result = _result(
        fault_type="static_route_misconfig",
        evidence=[
            "The device config contains ip route 192.0.2.6/32 198.51.100.2; "
            "the next-hop is a client endpoint and is non-routing.",
            "The route table confirms the static route is selected and installed via Ethernet16.",
        ],
    )

    assessment = BaseAgentReliability().assess(result)

    assert assessment.status == BaseAgentStatus.VALID
    assert assessment.direct_evidence


@pytest.mark.parametrize(
    "observation",
    [
        "spine3 get_device_interfaces shows Ethernet8 oper:down admin:down",
        "spine3:Ethernet12 oper=down admin=down",
        "port Ethernet12 admin status to down was observed in the device log",
    ],
)
def test_explicit_link_down_tool_formats_are_direct_evidence(observation):
    result = _result(
        fault_type="link_down",
        device="spine3",
        interface="Ethernet12",
        evidence=[observation],
    )

    assessment = BaseAgentReliability().assess(result)

    assert assessment.status == BaseAgentStatus.VALID
    assert assessment.direct_evidence


@pytest.mark.parametrize(
    "observation",
    [
        "Ethernet12 admin=up oper=up",
        "The link may be down but no interface state was observed.",
    ],
)
def test_link_down_without_observed_down_state_remains_low_evidence(observation):
    result = _result(
        fault_type="link_down",
        device="spine3",
        interface="Ethernet12",
        evidence=[observation],
    )

    assessment = BaseAgentReliability().assess(result)

    assert assessment.status == BaseAgentStatus.LOW_EVIDENCE
    assert not assessment.direct_evidence


@pytest.mark.parametrize(
    "evidence",
    [
        "Traffic is unreachable and a route policy might be involved.",
        "The affected prefix appears missing, but configuration was not observed.",
        "BGP is established; more checks are required.",
    ],
)
def test_speculative_route_policy_prose_is_not_direct_evidence(evidence):
    assessment = BaseAgentReliability().assess(_result(evidence=[evidence]))

    assert assessment.status == BaseAgentStatus.LOW_EVIDENCE
    assert not assessment.direct_evidence


def test_link_label_conflicts_with_observed_peer_container_not_running():
    result = _result(
        fault_type="link_down",
        device="leaf5",
        interface="Ethernet4",
        evidence=["The peer device spine3 container is not running."],
    )

    assessment = BaseAgentReliability().assess(result)

    assert assessment.status == BaseAgentStatus.LOW_EVIDENCE
    assert assessment.semantic_conflict is True
