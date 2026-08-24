"""Configuration defaults for the selective diagnostic harness."""

from __future__ import annotations

from dataclasses import dataclass, field

from netopsbench.evaluator.fault_type_judge import canonicalize_fault_type, supported_fault_types


def _registered_fault_label(alias: str) -> str:
    """Resolve an alias through the live benchmark registry, failing closed."""
    canonical = canonicalize_fault_type(alias)
    return canonical if canonical in supported_fault_types() else alias


def default_fault_type_aliases() -> dict[str, str]:
    """Return harness aliases not already guaranteed by the evaluator."""
    return {
        "interface_flapping": "link_flapping",
        "link_flap": "link_flapping",
        "unstable_link": "link_flapping",
        "intermittent_link_failure": "link_flapping",
        "routing_policy_error": "route_policy_misconfig",
        "route_map_misconfiguration": "route_policy_misconfig",
        "prefix_filter_error": "route_policy_misconfig",
        "missing_bgp_network_statement": "route_policy_misconfig",
        "missing_network_statement": "route_policy_misconfig",
        "mtu_misconfig": "mtu_mismatch",
        "mtu_misconfiguration": "mtu_mismatch",
        "acl_deny": "acl_misconfig",
        "acl_block": _registered_fault_label("acl_misconfiguration"),
    }


@dataclass(frozen=True)
class NormalizationConfig:
    enabled: bool = True
    fault_type_aliases: dict[str, str] = field(default_factory=default_fault_type_aliases)
    interface_required_fault_types: frozenset[str] = frozenset(
        {
            "acl_misconfig",
            "high_latency",
            "link_down",
            "link_flapping",
            "mtu_mismatch",
            "packet_corruption",
            "packet_loss",
        }
    )


@dataclass(frozen=True)
class RouterConfig:
    enabled: bool = True
    fast_path_confidence: float = 0.85
    escalate_missing_interface: bool = True
    escalate_generic_impairment: bool = True
    escalate_healthy_with_weak_symptom: bool = True
    close_hypothesis_margin: float = 0.20
    # Routing scores select the next bounded verification family.  They are
    # deliberately separate from hypothesis probability and the final gate.
    family_signal_weights: dict[str, float] = field(
        default_factory=lambda: {
            "configuration_difference": 6.0,
            "route_presence": 5.0,
            "configured_static_route": 5.0,
            "observed_routing_entry": 5.0,
            "payload_integrity_failure": 5.0,
            "syslog_event": 4.5,
            "packet_size_threshold": 4.0,
            "latency_median": 3.0,
            "latency_p95": 3.0,
            "packet_loss_rate": 2.0,
            "transient_loss": 3.5,
        }
    )
    weak_loss_routing_threshold: float = 0.02
    direct_loss_routing_threshold: float = 0.10
    latency_routing_threshold_ms: float = 30.0
    # Initial verification plus one evidence-driven family transition.  This
    # prevents both one-shot lock-in and unbounded branch oscillation.
    max_family_replans: int = 1
    replan_min_family_score: float = 1.0


@dataclass(frozen=True)
class CacheConfig:
    interface_state_ttl_seconds: float = 10.0
    route_state_ttl_seconds: float = 10.0
    bgp_state_ttl_seconds: float = 10.0
    topology_ttl_seconds: float = 300.0
    active_probe_ttl_seconds: float = 0.0


@dataclass(frozen=True)
class FeatureConfig:
    enabled: bool = False


@dataclass(frozen=True)
class BudgetConfig:
    max_extra_tool_calls_per_hard_case: int = 12
    max_extra_llm_calls_per_hard_case: int = 3
    max_active_probes_per_case: int = 6
    max_probe_packets_per_case: int = 500
    max_harness_iterations: int = 6


@dataclass(frozen=True)
class AdaptiveBudgetConfig:
    """Evidence-triggered ceiling above the normal hard-case budget.

    The normal 12-call/6-probe/500-packet limits remain the default execution
    envelope.  A grant is possible only when the orchestrator has already run
    the normal discriminator, the gate still cannot submit, and public
    evidence identifies a safe action that can resolve a concrete ambiguity.
    """

    enabled: bool = True
    # The adaptive ceiling follows one unresolved failure domain, not the
    # total topology size.  Four extra calls cover a four-way fabric but
    # deterministically miss members in 8/16-way ECMP.  The absolute ceiling
    # covers both ends of a 32-way fabric cut; the scale policy applies lower
    # per-tier limits and marks wider cuts for hierarchical localization.
    max_extra_tool_calls: int = 64
    max_extra_active_probes: int = 64
    max_extra_probe_packets: int = 1280
    max_ecmp_probe_candidates: int = 4
    max_failure_domain_candidates: int = 64
    semantic_route_extra_tool_calls: int = 64


@dataclass(frozen=True)
class ScalePolicyConfig:
    """Topology-derived investigation widths for different ECMP domains.

    Scale is intentionally based on the largest *local* attachment failure
    domain, not on the total number of unrelated clients or switches.  The
    policy changes candidate recall and path sampling only; final ranking and
    diagnosability thresholds remain global invariants.
    """

    enabled: bool = True
    small_max_ecmp_width: int = 4
    medium_max_ecmp_width: int = 8
    large_max_ecmp_width: int = 16
    small_failure_domain_cap: int = 8
    medium_failure_domain_cap: int = 16
    large_failure_domain_cap: int = 32
    # Directly cover a two-ended 32-way fabric cut. Wider local domains switch
    # to hierarchical localization rather than growing with total inventory.
    xlarge_failure_domain_cap: int = 64
    # Compatibility field names retained for existing configs. These are
    # topology-tier growth targets; the effective value never drops below
    # TopologyRankerConfig.max_ecmp_paths, so 8/16 are not literal hard caps.
    small_path_sample_cap: int = 8
    medium_path_sample_cap: int = 16
    large_path_sample_cap: int = 32
    xlarge_path_sample_cap: int = 64


@dataclass(frozen=True)
class BranchBudgetConfig:
    """Caps for orchestration stages inside the unchanged hard-case total."""

    semantic_closure_max_tool_calls: int = 6
    temporal_triage_max_tool_calls: int = 3
    # One peer interface query plus two independent peer-liveness probes can
    # follow the initial local interface observation on a device-wide outage.
    # This is a stage cap inside the unchanged per-case total budget.
    link_state_triage_max_tool_calls: int = 4
    # A large topology needs four control-plane/state reads plus up to eight
    # integrity sentinels (one per two attachment domains). This remains inside the
    # unchanged global 12-call hard-case budget.
    healthy_verification_max_tool_calls: int = 12
    final_verification_reserve_tool_calls: int = 0
    static_route_required_evidence_reserve_tool_calls: int = 4


@dataclass(frozen=True)
class OperationalVerificationConfig:
    """Bounded history and coverage policy for operational closures."""

    # Flaps can settle immediately before the diagnostic episode begins.
    # Query a small retained-event grace window, while still requiring the
    # normal independent transition contract before submission.
    temporal_lookback_seconds: int = 90
    # A healthy verdict is scoped to global service reachability plus
    # stratified payload-integrity sampling. Require both a minimum number of
    # independent pairs and a topology-relative number of attachment domains.
    healthy_min_integrity_pairs: int = 3
    healthy_min_attachment_coverage_ratio: float = 0.10


@dataclass(frozen=True)
class PacketLossProbeConfig:
    packets_per_pair: int = 30
    repeat_rounds: int = 2
    matrix_packets_per_round: int = 10
    matrix_repeat_rounds: int = 2
    # ECMP isolation is deliberately breadth-first: observe every member once
    # before spending another call confirming an anomaly.  Twenty packets is
    # the largest count supported by the underlying link-ping call and gives a
    # useful first-pass estimate without starving a later ECMP member.
    link_breadth_packets_per_pair: int = 20
    link_breadth_rounds: int = 1
    control_repeat_rounds: int = 1
    anomaly_pairs: int = 1
    control_pairs: int = 1
    link_isolation_enabled: bool = True
    warning_threshold: float = 0.10
    strong_threshold: float = 0.20
    max_pairs: int = 8
    timeout_seconds: float = 35.0


@dataclass(frozen=True)
class MTUProbeConfig:
    payload_sizes: tuple[int, ...] = (64, 512, 1200, 1372, 1400, 1472, 8972)
    packets_per_size: int = 2
    max_pairs: int = 1
    timeout_seconds: float = 35.0


@dataclass(frozen=True)
class LatencyProbeConfig:
    samples_per_pair: int = 7
    max_pairs: int = 8
    anomaly_pairs: int = 1
    control_pairs: int = 1
    link_isolation_enabled: bool = True
    absolute_threshold_ms: float = 30.0
    relative_multiplier_threshold: float = 3.0
    timeout_seconds: float = 35.0


@dataclass(frozen=True)
class CorruptionProbeConfig:
    samples_per_pair: int = 20
    # Forty packets per direction keeps a four-member ECMP integrity sweep
    # within the existing 500-packet case budget while reducing the miss
    # probability of an 8% stochastic corruption fault to about 3.6% on the
    # affected direction.  This is observation quality, not a gate change.
    samples_per_link_direction: int = 40
    timeout_seconds: float = 35.0
    allow_interface_counter_fallback: bool = True


@dataclass(frozen=True)
class TopologyRankerConfig:
    abnormal_path_weight: float = 2.0
    healthy_path_weight: float = -1.0
    active_probe_weight: float = 1.5
    counter_anomaly_weight: float = 1.5
    configuration_difference_weight: float = 1.0
    initial_device_bonus: float = 0.35
    initial_interface_bonus: float = 0.60
    # Candidate generation is intentionally recall-oriented. Submission uses
    # the independent, stricter thresholds in DiagnosabilityConfig.
    candidate_generation_threshold: float = 0.05
    candidate_top_k: int = 6
    # Compatibility override for callers of the Phase 4 prototype.
    minimum_candidate_score: float | None = None
    max_ecmp_paths: int = 32
    abnormal_loss_threshold: float = 0.05
    healthy_loss_threshold: float = 0.01
    abnormal_latency_ms: float = 30.0


@dataclass(frozen=True)
class DiagnosabilityConfig:
    submit_confidence: float = 0.75
    submit_margin: float = 0.20
    minimum_independent_evidence_sources: int = 2
    minimum_interface_score: float = 0.15
    minimum_interface_margin: float = 0.05


def default_impairment_matrix() -> dict[str, dict[str, float]]:
    return {
        "random_packet_loss": {
            "packet_loss": 3.0,
            "packet_corruption": 1.0,
            "mtu_mismatch": -1.0,
            "high_latency": -1.0,
        },
        "healthy_packet_delivery": {
            "packet_loss": -1.0,
            "packet_corruption": -0.5,
            "mtu_mismatch": 0.0,
            "high_latency": 0.0,
        },
        "payload_integrity_failure": {
            "packet_loss": -3.0,
            "packet_corruption": 5.0,
            "mtu_mismatch": -2.0,
            "high_latency": -2.0,
        },
        "size_dependent_failure": {
            "packet_loss": -2.0,
            "packet_corruption": -2.0,
            "mtu_mismatch": 5.0,
            "high_latency": -2.0,
        },
        "size_sweep_healthy": {
            "packet_loss": 0.0,
            "packet_corruption": 0.0,
            "mtu_mismatch": -4.0,
            "high_latency": 0.0,
        },
        "latency_anomaly": {
            "packet_loss": -2.0,
            "packet_corruption": -2.0,
            "mtu_mismatch": -2.0,
            "high_latency": 5.0,
        },
        "latency_healthy": {
            "packet_loss": 0.0,
            "packet_corruption": 0.0,
            "mtu_mismatch": 0.0,
            "high_latency": -2.0,
        },
        "configuration_difference": {
            "packet_loss": 0.5,
            "packet_corruption": 0.0,
            "mtu_mismatch": 2.0,
            "high_latency": 0.5,
        },
    }


@dataclass(frozen=True)
class HypothesisConfig:
    prior: float = 0.0
    matrix: dict[str, dict[str, float]] = field(default_factory=default_impairment_matrix)
    loss_warning_threshold: float = 0.05
    latency_threshold_ms: float = 30.0
    # A received payload whose application checksum is invalid is logically
    # incompatible with pure packet loss.  Keep this as a score-domain
    # constraint rather than allowing correlated missing-packet summaries to
    # overwhelm the integrity observation by row count.
    corruption_dominance_score_margin: float = 2.0
    # A stable DF size threshold plus an MTU difference on a candidate link
    # is a causal discriminator against size-independent packet loss.  Keep
    # this separate from the matrix so unrelated loss rows cannot outvote the
    # two-source MTU mechanism merely because a topology exposes more paths.
    mtu_dominance_score_margin: float = 2.0


@dataclass(frozen=True)
class TelemetryConfig:
    enabled: bool = False
    output_directory: str = "scenario_results/diagnostic_harness"


@dataclass(frozen=True)
class HarnessConfig:
    enabled: bool = True
    normalization: NormalizationConfig = field(default_factory=NormalizationConfig)
    hard_case_router: RouterConfig = field(default_factory=RouterConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    adaptive_budget: AdaptiveBudgetConfig = field(default_factory=AdaptiveBudgetConfig)
    scale_policy: ScalePolicyConfig = field(default_factory=ScalePolicyConfig)
    branch_budget: BranchBudgetConfig = field(default_factory=BranchBudgetConfig)
    operational_verification: OperationalVerificationConfig = field(default_factory=OperationalVerificationConfig)
    packet_loss_probe: PacketLossProbeConfig = field(default_factory=PacketLossProbeConfig)
    mtu_probe: MTUProbeConfig = field(default_factory=MTUProbeConfig)
    latency_probe: LatencyProbeConfig = field(default_factory=LatencyProbeConfig)
    corruption_probe: CorruptionProbeConfig = field(default_factory=CorruptionProbeConfig)
    topology_ranker_config: TopologyRankerConfig = field(default_factory=TopologyRankerConfig)
    hypothesis: HypothesisConfig = field(default_factory=HypothesisConfig)
    diagnosability: DiagnosabilityConfig = field(default_factory=DiagnosabilityConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    impairment_probes: FeatureConfig = field(default_factory=FeatureConfig)
    topology_ranker: FeatureConfig = field(default_factory=FeatureConfig)
    diagnosability_gate: FeatureConfig = field(default_factory=FeatureConfig)


__all__ = [
    "AdaptiveBudgetConfig",
    "BranchBudgetConfig",
    "BudgetConfig",
    "CacheConfig",
    "CorruptionProbeConfig",
    "DiagnosabilityConfig",
    "FeatureConfig",
    "HarnessConfig",
    "HypothesisConfig",
    "LatencyProbeConfig",
    "MTUProbeConfig",
    "NormalizationConfig",
    "OperationalVerificationConfig",
    "PacketLossProbeConfig",
    "RouterConfig",
    "ScalePolicyConfig",
    "TopologyRankerConfig",
    "TelemetryConfig",
    "default_impairment_matrix",
    "default_fault_type_aliases",
]
