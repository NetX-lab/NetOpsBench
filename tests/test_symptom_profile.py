from datetime import UTC, datetime

from examples.agents.diagnostic_harness.evidence import EvidenceStore
from examples.agents.diagnostic_harness.models import Evidence, EvidenceOrigin
from examples.agents.diagnostic_harness.routing import build_symptom_profile


def _evidence(evidence_id: str, category: str, value, **kwargs) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        entity_type=kwargs.pop("entity_type", "path"),
        entity_id=kwargs.pop("entity_id", "client1--client2"),
        category=category,
        value=value,
        source=kwargs.pop("source", "test"),
        timestamp=datetime.now(UTC),
        origin=kwargs.pop("origin", EvidenceOrigin.LIVE_TELEMETRY),
        **kwargs,
    )


def test_exact_directional_latency_outranks_weak_aggregate_loss():
    store = EvidenceStore(
        [
            _evidence(
                "weak-loss",
                "packet_loss_rate",
                0.03,
                independence_key="episode",
                metadata={"aggregate_pingmesh": True, "weak_performance_symptom": True},
            ),
            _evidence(
                "link-latency",
                "latency_median",
                120.0,
                independence_key="link-direction",
                covered_links=("leaf1:Ethernet0--spine1:Ethernet0",),
                path_observation_confidence=1.0,
                metadata={"category_anomaly": True},
            ),
        ]
    )

    profile = build_symptom_profile(store)

    assert profile.primary_family == "high_latency"
    assert profile.family_evidence_ids["high_latency"] == ("link-latency",)
    assert profile.margin > 0


def test_direct_semantic_difference_outranks_downstream_loss():
    store = EvidenceStore(
        [
            _evidence(
                "route-policy",
                "configuration_difference",
                {"prefix": "203.0.113.0/24"},
                entity_type="device",
                entity_id="leaf1",
                origin=EvidenceOrigin.CONFIG_READ,
                independence_key="config:leaf1",
                metadata={"semantic_family": "route_policy"},
            ),
            _evidence("loss", "packet_loss_rate", 1.0, independence_key="episode"),
        ]
    )

    assert build_symptom_profile(store).primary_family == "route_policy"


def test_bare_bgp_rib_miss_does_not_outrank_direct_latency():
    store = EvidenceStore(
        [
            _evidence(
                "rib-miss",
                "route_presence",
                {"prefix": "203.0.113.0/24", "present": False},
                entity_type="device",
                entity_id="leaf1",
                supports_submission=False,
                metadata={"missing_bgp_route": True, "route_semantic_candidate": True},
            ),
            _evidence(
                "latency",
                "latency_p95",
                80.0,
                metadata={"absolute_anomaly": True},
            ),
        ]
    )

    profile = build_symptom_profile(store)

    assert profile.primary_family == "high_latency"
    assert "route_policy" not in profile.family_scores


def test_correlated_rows_count_once_per_family_and_source():
    store = EvidenceStore(
        [
            _evidence("loss-1", "packet_loss_rate", 0.4, independence_key="pingmesh:episode"),
            _evidence("loss-2", "packet_loss_rate", 0.8, independence_key="pingmesh:episode"),
        ]
    )

    profile = build_symptom_profile(store)

    assert profile.family_scores == {"packet_loss": 2.0}
    assert profile.family_evidence_ids == {"packet_loss": ("loss-1",)} or profile.family_evidence_ids == {
        "packet_loss": ("loss-2",)
    }


def test_healthy_or_failed_observations_do_not_create_fault_family():
    store = EvidenceStore(
        [
            _evidence("healthy-loss", "packet_loss_rate", 0.0),
            _evidence("healthy-latency", "latency_median", 3.0),
            _evidence("tool-error", "tool_error", "timeout", reliability=0.0),
        ]
    )

    assert build_symptom_profile(store).primary_family is None


def test_planning_only_mtu_outlier_selects_verification_family_not_fault_proof():
    item = _evidence(
        "mtu-outlier",
        "configuration_difference",
        {"local_mtu": 1400, "device_mode_mtu": 9100, "different": True},
        entity_type="interface",
        entity_id="spine2:Ethernet12",
        origin=EvidenceOrigin.CONFIG_READ,
        supports_submission=False,
        metadata={"semantic_family": "mtu", "planning_only": True},
    )

    profile = build_symptom_profile(EvidenceStore([item]))

    assert profile.primary_family == "mtu"


def test_unverified_mtu_suspect_yields_to_independent_material_loss():
    store = EvidenceStore(
        [
            _evidence(
                "mtu-suspect",
                "packet_size_threshold",
                {"size_dependent_failure": True, "threshold_payload_size": None},
                supports_submission=False,
                metadata={"planning_only": True},
                independence_key="episode-window",
            ),
            _evidence(
                "ordinary-loss",
                "packet_loss_rate",
                0.20,
                independence_key="episode-window",
            ),
        ]
    )

    profile = build_symptom_profile(store)

    assert profile.primary_family == "packet_loss"
    assert profile.family_scores["packet_loss"] > profile.family_scores["mtu"]


def test_unverified_mtu_suspect_still_selects_sweep_without_ordinary_loss():
    suspect = _evidence(
        "mtu-suspect",
        "packet_size_threshold",
        {"size_dependent_failure": True, "threshold_payload_size": None},
        supports_submission=False,
        metadata={"planning_only": True},
    )

    assert build_symptom_profile(EvidenceStore([suspect])).primary_family == "mtu"
