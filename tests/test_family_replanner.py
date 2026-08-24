from datetime import UTC, datetime

from examples.agents.diagnostic_harness.config import RouterConfig
from examples.agents.diagnostic_harness.evidence import EvidenceStore
from examples.agents.diagnostic_harness.models import Evidence, EvidenceOrigin
from examples.agents.diagnostic_harness.orchestrator import _consumes_causal_family_replan
from examples.agents.diagnostic_harness.routing import BoundedFamilyReplanner


def _evidence(evidence_id: str, category: str, value, **kwargs) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        entity_type="path",
        entity_id="client1--client2",
        category=category,
        value=value,
        source="live-test",
        timestamp=datetime.now(UTC),
        origin=kwargs.pop("origin", EvidenceOrigin.LIVE_TELEMETRY),
        **kwargs,
    )


def test_disproved_operational_family_can_replan_to_observed_latency():
    store = EvidenceStore(
        [
            _evidence(
                "latency",
                "latency_median",
                90.0,
                metadata={"category_anomaly": True},
                independence_key="directional-rtt",
            )
        ]
    )

    decision = BoundedFamilyReplanner().select(
        store,
        current_family="link_state_verification",
        attempted_families={"link_state_verification"},
        transitions=0,
        allowed_families=frozenset({"high_latency", "packet_loss"}),
    )

    assert decision is not None
    assert decision.family == "high_latency"
    assert decision.evidence_ids == ("latency",)


def test_runtime_semantic_verification_preserves_one_causal_replan_for_loss():
    store = EvidenceStore(
        [
            _evidence(
                "material-loss",
                "packet_loss_rate",
                0.30,
                independence_key="episode-loss-window",
            )
        ]
    )

    assert not _consumes_causal_family_replan("runtime_semantic")
    decision = BoundedFamilyReplanner().select(
        store,
        current_family="runtime_semantic",
        attempted_families={"link_state_verification", "runtime_semantic"},
        transitions=0,
        allowed_families=frozenset({"packet_loss", "packet_corruption"}),
    )

    assert decision is not None
    assert decision.family == "packet_loss"
    assert _consumes_causal_family_replan(decision.family)


def test_base_prose_cannot_drive_replan():
    store = EvidenceStore(
        [
            _evidence(
                "claim",
                "packet_loss_rate",
                1.0,
                origin=EvidenceOrigin.BASE_CLAIM,
            )
        ]
    )

    assert (
        BoundedFamilyReplanner().select(
            store,
            current_family="route_policy",
            attempted_families={"route_policy"},
            transitions=0,
            allowed_families=frozenset({"packet_loss"}),
        )
        is None
    )


def test_unverified_df_suspect_does_not_override_independent_packet_loss():
    evidence = [
        _evidence(
            "mtu-suspect",
            "packet_size_threshold",
            {"size_dependent_failure": True, "threshold_payload_size": None},
            supports_submission=False,
            metadata={"planning_only": True},
        )
    ]
    evidence.extend(
        _evidence(
            f"loss-{index}",
            "packet_loss_rate",
            0.30,
            independence_key=f"loss-batch-{index}",
        )
        for index in range(4)
    )

    decision = BoundedFamilyReplanner().select(
        EvidenceStore(evidence),
        current_family="route_policy",
        attempted_families={"route_policy"},
        transitions=0,
        allowed_families=frozenset({"mtu", "packet_loss"}),
    )

    assert decision is not None
    assert decision.family == "packet_loss"
    assert decision.evidence_ids[0].startswith("loss-")


def test_verified_size_threshold_replans_before_higher_volume_loss_rows():
    evidence = [
        _evidence(
            "mtu-threshold",
            "packet_size_threshold",
            {
                "size_dependent_failure": True,
                "largest_successful_payload_size": 1372,
                "smallest_failed_payload_size": 1400,
            },
        )
    ]
    evidence.extend(
        _evidence(
            f"loss-{index}",
            "packet_loss_rate",
            0.30,
            independence_key=f"loss-batch-{index}",
        )
        for index in range(4)
    )

    decision = BoundedFamilyReplanner().select(
        EvidenceStore(evidence),
        current_family="route_policy",
        attempted_families={"route_policy"},
        transitions=0,
        allowed_families=frozenset({"mtu", "packet_loss"}),
    )

    assert decision is not None
    assert decision.family == "mtu"
    assert decision.evidence_ids == ("mtu-threshold",)


def test_replanner_never_oscillates_or_exceeds_transition_cap():
    store = EvidenceStore([_evidence("loss", "packet_loss_rate", 0.5)])
    replanner = BoundedFamilyReplanner(RouterConfig(max_family_replans=1))

    assert (
        replanner.select(
            store,
            current_family="high_latency",
            attempted_families={"high_latency", "packet_loss"},
            transitions=0,
            allowed_families=frozenset({"packet_loss"}),
        )
        is None
    )
    assert (
        replanner.select(
            store,
            current_family="high_latency",
            attempted_families={"high_latency"},
            transitions=1,
            allowed_families=frozenset({"packet_loss"}),
        )
        is None
    )
