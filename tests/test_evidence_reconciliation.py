from datetime import UTC, datetime, timedelta

from examples.agents.diagnostic_harness.evidence import reconcile_active_path_coverage
from examples.agents.diagnostic_harness.models import Evidence, EvidenceOrigin


def _healthy(link_id: str, *, evidence_id: str, timestamp: datetime) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        entity_type="path",
        entity_id=link_id,
        category="packet_loss_rate",
        value=0.0,
        source="ping_link_test",
        timestamp=timestamp,
        origin=EvidenceOrigin.ACTIVE_PROBE,
        probe_id="link-screen",
        covered_links=(link_id,),
        observed_path=(link_id,),
        path_observation_confidence=1.0,
        independence_key=f"probe:{evidence_id}",
        metadata={"sent": 20, "rounds": 1},
    )


def _anomaly(timestamp: datetime) -> Evidence:
    return Evidence(
        evidence_id="passive-loss",
        entity_type="path",
        entity_id="leaf1--leaf2",
        category="packet_loss_rate",
        value=0.25,
        source="pingmesh_episode",
        timestamp=timestamp,
        origin=EvidenceOrigin.PUBLIC_OBSERVATION,
        possible_paths=(("link-a",), ("link-b",)),
        independence_key="pingmesh:episode",
    )


def test_complete_newer_exact_link_coverage_demotes_passive_path_symptom():
    observed_at = datetime.now(UTC)
    evidence = [
        _anomaly(observed_at),
        _healthy("link-a", evidence_id="healthy-a", timestamp=observed_at + timedelta(seconds=1)),
        _healthy("link-b", evidence_id="healthy-b", timestamp=observed_at + timedelta(seconds=2)),
    ]

    reconciled = reconcile_active_path_coverage(evidence, warning_threshold=0.10)

    anomaly = next(item for item in reconciled if item.evidence_id == "passive-loss")
    assert anomaly.supports_submission is False
    assert anomaly.usable_for_planning is True
    assert anomaly.metadata["reconciled_by_complete_active_path_coverage"] is True
    assert any(item.category == "coverage_certificate" for item in reconciled)


def test_partial_ecmp_coverage_never_demotes_unknown_path_symptom():
    observed_at = datetime.now(UTC)
    reconciled = reconcile_active_path_coverage(
        [
            _anomaly(observed_at),
            _healthy("link-a", evidence_id="healthy-a", timestamp=observed_at + timedelta(seconds=1)),
        ],
        warning_threshold=0.10,
    )

    anomaly = next(item for item in reconciled if item.evidence_id == "passive-loss")
    assert anomaly.supports_submission is True
    assert not any(item.category == "coverage_certificate" for item in reconciled)


def test_older_active_observation_cannot_refute_newer_passive_symptom():
    observed_at = datetime.now(UTC)
    reconciled = reconcile_active_path_coverage(
        [
            _anomaly(observed_at),
            _healthy("link-a", evidence_id="healthy-a", timestamp=observed_at - timedelta(seconds=2)),
            _healthy("link-b", evidence_id="healthy-b", timestamp=observed_at - timedelta(seconds=1)),
        ],
        warning_threshold=0.10,
    )

    assert next(item for item in reconciled if item.evidence_id == "passive-loss").supports_submission is True
