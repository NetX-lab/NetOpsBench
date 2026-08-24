from datetime import UTC, datetime

from examples.agents.diagnostic_harness.hypotheses import (
    DiagnosabilityGate,
    DiagnosisReducer,
    HypothesisScorer,
    IndependentFault,
    MultiFaultAnalyzer,
)
from examples.agents.diagnostic_harness.models import Evidence, EvidenceOrigin, RankedInterfaceCandidate
from examples.agents.diagnostic_harness.orchestrator import DiagnosticHarness

from .diagnostic_harness_helpers import diagnosis_result


def _candidate(link_id: str, device: str = "leaf1", interface: str = "Ethernet0"):
    return RankedInterfaceCandidate(
        link_id=link_id,
        primary_device=device,
        primary_interface=interface,
        peer_device="spine1",
        peer_interface="Ethernet0",
        score=0.8,
    )


def test_independent_mtu_fault_is_reported_as_secondary_root_cause():
    link_id = "leaf1:Ethernet0--spine1:Ethernet0"
    evidence = [
        Evidence(
            evidence_id="mtu-threshold",
            entity_type="interface",
            entity_id="leaf1:Ethernet0",
            category="packet_size_threshold",
            value={"size_dependent_failure": True},
            source="ping_link_test_df_size_sweep",
            timestamp=datetime.now(UTC),
            origin=EvidenceOrigin.ACTIVE_PROBE,
            independence_key="probe:mtu-link",
            covered_links=(link_id,),
            path_observation_confidence=1.0,
            metadata={
                "fault_endpoint_device": "leaf1",
                "fault_endpoint_interface": "Ethernet0",
            },
        ),
        Evidence(
            evidence_id="mtu-config",
            entity_type="interface",
            entity_id="leaf1:Ethernet0",
            category="configuration_difference",
            value={"field": "mtu", "different": True, "local_mtu": 1400, "peer_mtu": 9100},
            source="get_device_interfaces",
            timestamp=datetime.now(UTC),
            origin=EvidenceOrigin.CONFIG_READ,
            independence_key="tool:get_device_interfaces:leaf1",
            metadata={"link_id": link_id},
        ),
    ]

    faults = MultiFaultAnalyzer().detect(
        evidence,
        [_candidate(link_id)],
        primary_fault_type="packet_loss",
        primary_device="leaf9",
        primary_interface="Ethernet4",
    )

    assert len(faults) == 1
    assert faults[0].fault_type == "mtu_mismatch"
    assert faults[0].device == "leaf1"
    assert set(faults[0].evidence_ids) == {"mtu-threshold", "mtu-config"}


def test_base_prose_and_unscoped_symptoms_cannot_create_secondary_fault():
    evidence = [
        Evidence(
            evidence_id="base-loss",
            entity_type="path",
            entity_id="client1--client2",
            category="packet_loss_rate",
            value=1.0,
            source="base_agent",
            timestamp=datetime.now(UTC),
            origin=EvidenceOrigin.BASE_CLAIM,
            independence_key="base",
            metadata={"rounds": 2, "sent": 60},
        )
    ]

    assert not MultiFaultAnalyzer().detect(
        evidence,
        [_candidate("leaf1:Ethernet0--spine1:Ethernet0")],
        primary_fault_type="high_latency",
        primary_device="leaf1",
        primary_interface="Ethernet0",
    )


def test_same_link_corruption_is_competing_explanation_not_secondary_loss_fault():
    """One failure domain must not donate its decisive evidence to a secondary.

    Packet loss is an observable consequence of corruption.  Treating the
    corruption observation as an independent secondary fault removes the only
    discriminating evidence before the primary hypothesis is re-scored.
    """
    link_id = "leaf1:Ethernet4--spine2:Ethernet0"
    evidence = [
        Evidence(
            evidence_id="link-loss",
            entity_type="path",
            entity_id="leaf1--spine2",
            category="packet_loss_rate",
            value=0.15,
            source="ping_test_repeated",
            timestamp=datetime.now(UTC),
            origin=EvidenceOrigin.ACTIVE_PROBE,
            independence_key="probe:loss",
            covered_links=(link_id,),
            observed_path=(link_id,),
            path_observation_confidence=1.0,
            metadata={"rounds": 2, "sent": 40},
        ),
        Evidence(
            evidence_id="checksum-failure",
            entity_type="path",
            entity_id="leaf1--spine2",
            category="payload_integrity_failure",
            value=True,
            source="payload_integrity_link_test",
            timestamp=datetime.now(UTC),
            origin=EvidenceOrigin.ACTIVE_PROBE,
            independence_key="probe:integrity",
            covered_links=(link_id,),
            observed_path=(link_id,),
            path_observation_confidence=1.0,
            metadata={
                "fault_endpoint_device": "leaf1",
                "fault_endpoint_interface": "Ethernet4",
            },
        ),
    ]

    faults = MultiFaultAnalyzer().detect(
        evidence,
        [_candidate(link_id, device="leaf1", interface="Ethernet4")],
        primary_fault_type="packet_loss",
        primary_device="leaf1",
        primary_interface="Ethernet4",
        primary_link_id=link_id,
    )

    assert faults == ()

    reducer = DiagnosisReducer(HypothesisScorer(), DiagnosabilityGate(), MultiFaultAnalyzer())
    reduction = reducer.reduce(evidence, [_candidate(link_id, device="leaf1", interface="Ethernet4")])
    top = max(reduction.hypotheses.values(), key=lambda item: item.probability)
    assert top.fault_type == "packet_corruption"
    assert "checksum-failure" in top.supporting_evidence


def test_semantic_secondary_requires_direct_config_and_independent_consequence():
    evidence = [
        Evidence(
            evidence_id="policy-config",
            entity_type="device",
            entity_id="leaf2",
            category="configuration_difference",
            value={"difference": "explicit_prefix_deny"},
            source="get_device_config",
            timestamp=datetime.now(UTC),
            origin=EvidenceOrigin.CONFIG_READ,
            independence_key="tool:config:leaf2",
            metadata={"semantic_family": "route_policy"},
        ),
        Evidence(
            evidence_id="policy-route",
            entity_type="device",
            entity_id="leaf2",
            category="route_presence",
            value={"route_count": 0},
            source="get_route_table",
            timestamp=datetime.now(UTC),
            origin=EvidenceOrigin.LIVE_TELEMETRY,
            independence_key="tool:route:leaf2",
            metadata={"semantic_family": "route_policy"},
        ),
    ]

    faults = MultiFaultAnalyzer().detect(
        evidence,
        [],
        primary_fault_type="packet_loss",
        primary_device="leaf1",
        primary_interface="Ethernet0",
    )

    assert [(item.fault_type, item.device) for item in faults] == [("route_policy_misconfig", "leaf2")]


def test_secondary_faults_extend_findings_without_replacing_primary_result():
    primary = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "packet_loss",
            "device": "leaf1",
            "interface": "Ethernet0",
            "confidence": 0.9,
        }
    )
    secondary = IndependentFault(
        fault_type="mtu_mismatch",
        device="leaf2",
        interface="Ethernet4",
        link_id="leaf2:Ethernet4--spine2:Ethernet0",
        confidence=0.88,
        evidence_ids=("mtu-size", "mtu-config"),
    )

    result = DiagnosticHarness._with_secondary_faults(primary, (secondary,))

    assert result.findings["fault_type"] == "packet_loss"
    assert result.findings["location"] == {"device": "leaf1", "interface": "Ethernet0"}
    assert result.findings["additional_faults"] == [secondary.as_finding()]
