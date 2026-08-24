from datetime import UTC, datetime

from examples.agents.diagnostic_harness.models import Evidence, EvidenceOrigin
from examples.agents.diagnostic_harness.probes.planner import DeterministicProbePlanner


def _latency(source: str) -> Evidence:
    return Evidence(
        evidence_id=f"latency:{source}",
        entity_type="path",
        entity_id="leaf1--spine1",
        category="latency_median",
        value=80.0,
        source=source,
        timestamp=datetime.now(UTC),
        origin=EvidenceOrigin.LIVE_TELEMETRY,
        metadata={"category_anomaly": True},
    )


def test_base_tool_observation_keeps_independent_primary_probe_available():
    actions = DeterministicProbePlanner().plan(
        family="high_latency",
        evidence=[_latency("base_tool:latency_link_test")],
    )

    assert [item.action_id for item in actions] == ["rtt_matrix"]


def test_harness_primary_observation_prevents_duplicate_probe():
    actions = DeterministicProbePlanner().plan(
        family="high_latency",
        evidence=[_latency("rtt_matrix")],
    )

    assert actions == []
