from datetime import UTC, datetime

from examples.agents.diagnostic_harness.models import Evidence, EvidenceOrigin
from examples.agents.diagnostic_harness.verification.contracts import EvidenceContractEvaluator
from netopsbench.sdk.agents import DiagnosisResult


def _result(fault_type, *, verdict="fault_detected", device="leaf1", interface="Ethernet0"):
    return DiagnosisResult(
        agent_name="contract-test",
        verdict=verdict,
        confidence=0.95,
        findings={"fault_type": fault_type, "location": {"device": device, "interface": interface}},
    )


def _evidence(evidence_id, category, value, *, origin=EvidenceOrigin.LIVE_TELEMETRY, **metadata):
    entity_id = metadata.pop("entity_id", "leaf1:Ethernet0")
    return Evidence(
        evidence_id=evidence_id,
        entity_type="interface",
        entity_id=entity_id,
        category=category,
        value=value,
        source="collector",
        timestamp=datetime.now(UTC),
        origin=origin,
        independence_key=metadata.pop("independence_key", evidence_id),
        metadata=metadata,
    )


def _complete_healthy_observations():
    return [
        _evidence("loss", "packet_loss_rate", 0.0),
        _evidence("integrity", "payload_integrity_failure", False),
        _evidence("bgp", "bgp_neighbor_state", "ESTABLISHED"),
        _evidence("interface", "interface_oper_state", "up"),
        _evidence("route", "route_presence", True),
    ]


def _healthy_coverage_certificate(*, sufficient: bool):
    completed = 3 if sufficient else 2
    sampled = 6 if sufficient else 4
    return Evidence(
        evidence_id="coverage",
        entity_type="topology",
        entity_id="network",
        category="coverage_certificate",
        value={
            "healthy_scope": "service_reachability_with_stratified_integrity_sampling",
            "global_reachability_complete": True,
            "integrity_sampling_sufficient": sufficient,
            "integrity_pairs_completed": completed,
            "minimum_integrity_pairs": 3,
            "attachment_domains_sampled_for_integrity": sampled,
            "minimum_attachment_domains": 6,
        },
        source="healthy_verification",
        timestamp=datetime.now(UTC),
        origin=EvidenceOrigin.PUBLIC_OBSERVATION,
        supports_submission=False,
        metadata={"coverage_complete": True, "missing_data_is_healthy": False},
    )


def test_base_claim_cannot_satisfy_link_down_contract():
    claim = _evidence("claim", "interface_oper_state", "down", origin=EvidenceOrigin.BASE_CLAIM)

    decision = EvidenceContractEvaluator().evaluate(_result("link_down"), [claim])

    assert not decision.satisfied
    assert "direct_interface_state" in decision.missing_requirements


def test_one_sided_interface_state_requires_peer_scope_before_link_down_fast_path():
    live = _evidence("live", "interface_oper_state", "down", link_id="link-1")

    decision = EvidenceContractEvaluator().evaluate(_result("link_down"), [live])

    assert not decision.satisfied
    assert "peer_scope_liveness" in decision.missing_requirements


def test_peer_interface_observation_satisfies_link_down_scope_contract():
    evidence = [
        _evidence("local", "interface_oper_state", "down", link_id="link-1"),
        _evidence(
            "peer",
            "interface_oper_state",
            "up",
            entity_id="spine1:Ethernet0",
            link_id="link-1",
        ),
    ]

    assert EvidenceContractEvaluator().evaluate(_result("link_down"), evidence).satisfied


def test_corruption_requires_received_invalid_payload_not_error_counter():
    counter = _evidence("counter", "interface_counter_delta", {"in_errors": 10})
    invalid = EvidenceContractEvaluator().evaluate(_result("packet_corruption"), [counter])
    assert not invalid.satisfied
    assert "invalid_payload_observation" in invalid.missing_requirements


def test_healthy_contract_requires_integrity_sentinel():
    result = _result(None, verdict="network_healthy", device=None, interface=None)
    evidence = [
        _evidence("loss", "packet_loss_rate", 0.0),
        _evidence("bgp", "bgp_neighbor_state", "ESTABLISHED"),
        _evidence("interface", "interface_oper_state", "up"),
        _evidence("route", "route_presence", True),
    ]

    decision = EvidenceContractEvaluator().evaluate(result, evidence)

    assert not decision.satisfied
    assert "healthy_payload_integrity_observation" in decision.missing_requirements


def test_healthy_contract_requires_explicit_scope_certificate():
    result = _result(None, verdict="network_healthy", device=None, interface=None)

    decision = EvidenceContractEvaluator().evaluate(result, _complete_healthy_observations())

    assert not decision.satisfied
    assert "healthy_scope_coverage_certificate" in decision.missing_requirements


def test_healthy_contract_rejects_incomplete_integrity_coverage_certificate():
    result = _result(None, verdict="network_healthy", device=None, interface=None)
    certificate = _healthy_coverage_certificate(sufficient=False)

    decision = EvidenceContractEvaluator().evaluate(
        result,
        [*_complete_healthy_observations(), certificate],
    )

    assert not decision.satisfied
    assert "healthy_integrity_coverage_incomplete" in decision.missing_requirements


def test_healthy_contract_accepts_declared_bounded_scope_with_sufficient_sampling():
    result = _result(None, verdict="network_healthy", device=None, interface=None)
    certificate = _healthy_coverage_certificate(sufficient=True)

    assert EvidenceContractEvaluator().evaluate(
        result,
        [*_complete_healthy_observations(), certificate],
    ).satisfied


def test_healthy_contract_fails_closed_on_malformed_certificate_counts():
    result = _result(None, verdict="network_healthy", device=None, interface=None)
    certificate = _healthy_coverage_certificate(sufficient=True)
    certificate.value["integrity_pairs_completed"] = "unknown"

    decision = EvidenceContractEvaluator().evaluate(
        result,
        [*_complete_healthy_observations(), certificate],
    )

    assert not decision.satisfied
    assert "healthy_integrity_coverage_incomplete" in decision.missing_requirements


def test_acl_contract_uses_live_config_and_independent_dataplane_counter():
    evidence = [
        _evidence(
            "config",
            "configuration_difference",
            {"action": "drop"},
            semantic_family="acl",
            independence_key="acl-config",
        ),
        _evidence(
            "counter",
            "interface_counter_delta",
            {"drop_rule_packets": 4},
            independence_key="acl-counter",
        ),
    ]

    assert EvidenceContractEvaluator().evaluate(_result("acl_misconfig"), evidence).satisfied


def test_correlated_rows_from_one_probe_do_not_satisfy_independence_contract():
    evidence = [
        _evidence(
            "loss",
            "packet_loss_rate",
            0.25,
            independence_key="one-packet-batch",
            rounds=2,
        ),
        _evidence(
            "integrity",
            "payload_integrity_failure",
            False,
            independence_key="one-packet-batch",
        ),
    ]

    decision = EvidenceContractEvaluator().evaluate(_result("packet_loss"), evidence)

    assert not decision.satisfied
    assert "independent_observation_groups" in decision.missing_requirements


def test_two_independent_packet_batches_satisfy_loss_contract():
    evidence = [
        _evidence("loss-a", "packet_loss_rate", 0.25, independence_key="batch-a", rounds=2),
        _evidence("loss-b", "packet_loss_rate", 0.20, independence_key="batch-b", rounds=2),
    ]

    assert EvidenceContractEvaluator().evaluate(_result("packet_loss"), evidence).satisfied


def test_bgp_state_without_configuration_attribution_is_not_a_misconfig_proof():
    state = _evidence(
        "bgp-state",
        "bgp_neighbor_state",
        {"healthy": False, "neighbors": [{"state": "Idle"}]},
        semantic_family="bgp",
        direct_bgp_evidence=True,
    )

    decision = EvidenceContractEvaluator().evaluate(
        _result("bgp_neighbor_misconfig", interface=None),
        [state],
    )

    assert not decision.satisfied
    assert "direct_bgp_configuration_attribution" in decision.missing_requirements


def test_bgp_detail_configuration_error_closes_state_and_cause_contract():
    state = _evidence(
        "bgp-config",
        "bgp_neighbor_state",
        {"healthy": False, "neighbors": [{"state": "Idle", "last_error": "Bad Peer AS"}]},
        semantic_family="bgp",
        direct_bgp_evidence=True,
        direct_bgp_configuration_evidence=True,
        bgp_configuration_fault_reason="peer_as_mismatch",
    )

    assert EvidenceContractEvaluator().evaluate(
        _result("bgp_neighbor_misconfig", interface=None),
        [state],
    ).satisfied
