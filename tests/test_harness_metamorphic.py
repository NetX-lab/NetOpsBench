from datetime import UTC, datetime

from examples.agents.diagnostic_harness.evidence import EvidenceStore
from examples.agents.diagnostic_harness.models import BaseAgentAssessment, BaseAgentStatus, Evidence, EvidenceOrigin
from examples.agents.diagnostic_harness.routing import HardCaseRouter, build_symptom_profile
from netopsbench.sdk.agents import DiagnosisResult


def _result(reasoning: str) -> DiagnosisResult:
    return DiagnosisResult(
        agent_name="arbitrary-provider",
        verdict="inconclusive",
        findings={"fault_type": None, "location": {"device": None, "interface": None}, "evidence": []},
        confidence=0.0,
        reasoning=reasoning,
    )


def _latency(entity_id: str, evidence_id: str = "latency") -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        entity_type="path",
        entity_id=entity_id,
        category="latency_median",
        value=75.0,
        source="latency_link_test",
        timestamp=datetime.now(UTC),
        origin=EvidenceOrigin.LIVE_TELEMETRY,
        independence_key="directional-link-rtt",
        metadata={"category_anomaly": True},
    )


def test_provider_wording_cannot_change_observation_first_family():
    store = EvidenceStore([_latency("rack-a--rack-b")])
    assessment = BaseAgentAssessment(
        status=BaseAgentStatus.SCHEMA_FAILED,
        reasons=("invalid_schema",),
        direct_evidence=False,
        semantic_conflict=False,
    )

    left = HardCaseRouter().route(
        result=_result("The model guessed packet loss."),
        evidence_store=store,
        base_assessment=assessment,
    )
    right = HardCaseRouter().route(
        result=_result("No useful final answer was produced."),
        evidence_store=store,
        base_assessment=assessment,
    )

    assert left.family == right.family == "high_latency"


def test_device_renaming_does_not_change_symptom_classification():
    original = build_symptom_profile(EvidenceStore([_latency("leaf7--spine3")]))
    renamed = build_symptom_profile(EvidenceStore([_latency("rack-switch-z--fabric-node-q")]))

    assert original.family_scores == renamed.family_scores
    assert original.primary_family == renamed.primary_family == "high_latency"


def test_inventory_replication_does_not_multiply_one_observation_batch():
    one = build_symptom_profile(EvidenceStore([_latency("client1--client2", "row-1")]))
    many = build_symptom_profile(
        EvidenceStore([_latency(f"client{index}--client99", f"row-{index}") for index in range(1, 257)])
    )

    assert one.family_scores == many.family_scores


def test_base_claim_never_becomes_observation_when_provider_is_confident():
    claim = Evidence(
        evidence_id="model-claim",
        entity_type="path",
        entity_id="client1--client2",
        category="packet_loss_rate",
        value=1.0,
        source="base_agent_claim",
        timestamp=datetime.now(UTC),
        origin=EvidenceOrigin.BASE_CLAIM,
    )

    assert build_symptom_profile(EvidenceStore([claim])).primary_family is None
