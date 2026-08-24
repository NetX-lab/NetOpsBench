"""Shared deterministic models for the diagnostic harness."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class BaseAgentStatus(StrEnum):
    """Whether the base result may control routing and fast-path submission."""

    VALID = "valid"
    RECURSION_FAILED = "recursion_failed"
    SCHEMA_FAILED = "schema_failed"
    TOOL_LOOP_FAILED = "tool_loop_failed"
    TOOL_INFRASTRUCTURE_FAILED = "tool_infrastructure_failed"
    LOW_EVIDENCE = "low_evidence"


class EvidenceOrigin(StrEnum):
    """Provenance class for one observation.

    ``BASE_CLAIM`` is traceable and may seed verification, but it is never
    allowed to satisfy a submission contract or increase a hypothesis score.
    """

    BASE_CLAIM = "base_claim"
    LIVE_TELEMETRY = "live_telemetry"
    CONFIG_READ = "config_read"
    ACTIVE_PROBE = "active_probe"
    TOPOLOGY = "topology"
    PUBLIC_OBSERVATION = "public_observation"
    UNKNOWN = "unknown"


class EvidenceDirection(StrEnum):
    """Direction of a path or endpoint observation when it is known."""

    A_TO_B = "a_to_b"
    B_TO_A = "b_to_a"
    BIDIRECTIONAL = "bidirectional"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BaseAgentAssessment:
    status: BaseAgentStatus
    reasons: tuple[str, ...] = ()
    direct_evidence: bool = False
    semantic_conflict: bool = False


@dataclass(frozen=True)
class FaultFamilyHint:
    """A bounded semantic clue retained from an otherwise unreliable result."""

    family: str
    source: str
    supporting_observation_ids: tuple[str, ...] = ()
    device: str | None = None
    prefix: str | None = None
    next_hop: str | None = None
    route_table: str | None = None
    raw_excerpt_hash: str | None = None
    reliability: float = 0.0


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    entity_type: str
    entity_id: str
    category: str
    value: Any
    source: str
    timestamp: datetime | None
    reliability: float = 1.0
    freshness: float = 1.0
    probe_id: str | None = None
    raw_reference: str | None = None
    # Path scope is populated from the public topology after collection.  An
    # empty ``covered_links`` with multiple ``possible_paths`` deliberately
    # means that the actual ECMP member is unknown; it is not evidence that
    # every possible member was traversed.
    observed_path: tuple[str, ...] | None = None
    possible_paths: tuple[tuple[str, ...], ...] = ()
    covered_links: tuple[str, ...] = ()
    path_observation_confidence: float = 0.0
    origin: EvidenceOrigin = EvidenceOrigin.UNKNOWN
    # Median, p95, loss, and jitter from one packet batch share a key and count
    # as one independent observation in the discriminator.
    independence_key: str | None = None
    direction: EvidenceDirection = EvidenceDirection.UNKNOWN
    # Planning and submission are deliberately separate trust decisions.  A
    # structured observation copied from the base-agent tool trace may safely
    # narrow a follow-up query, while still requiring the harness to obtain an
    # independent live observation before it can submit a diagnosis.
    usable_for_planning: bool = True
    supports_submission: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Hypothesis:
    hypothesis_id: str
    fault_type: str
    device: str | None
    interface: str | None
    link_id: str | None
    score: float = 0.0
    probability: float = 0.0
    supporting_evidence: list[str] = field(default_factory=list)
    contradicting_evidence: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DiagnosisView:
    """Consistent read-only access to a diagnosis result's structured fields."""

    fault_type: str | None
    location: Mapping[str, Any]
    evidence: tuple[str, ...]

    @property
    def device(self) -> str | None:
        return self.location.get("device")

    @property
    def interface(self) -> str | None:
        return self.location.get("interface")

    @classmethod
    def from_result(cls, result: Any) -> DiagnosisView:
        findings = result.findings if isinstance(getattr(result, "findings", None), Mapping) else {}
        location = findings.get("location")
        raw_evidence = findings.get("evidence")
        evidence = (
            tuple(str(item) for item in raw_evidence)
            if isinstance(raw_evidence, Sequence) and not isinstance(raw_evidence, (str, bytes))
            else ()
        )
        return cls(
            fault_type=findings.get("fault_type"),
            location=location if isinstance(location, Mapping) else {},
            evidence=evidence,
        )


@dataclass
class DiagnosticState:
    initial_result: Any = None
    evidence: dict[str, Evidence] = field(default_factory=dict)
    hypotheses: dict[str, Hypothesis] = field(default_factory=dict)
    topology_graph: Any = None
    hard_case_family: str | None = None
    tool_calls: int = 0
    llm_calls: int = 0
    probe_packets: int = 0
    iterations: int = 0
    resolved: bool = False
    inconclusive_reason: str | None = None
    base_status: BaseAgentStatus = BaseAgentStatus.VALID


@dataclass(frozen=True)
class FaultTypeNormalization:
    original: str | None
    value: str | None
    is_canonical: bool
    normalized_from: str | None = None
    validation_error: str | None = None


@dataclass(frozen=True)
class InterfaceNormalization:
    device: str | None
    original: str | None
    value: str | None
    link_id: str | None = None
    validation_error: str | None = None


@dataclass(frozen=True)
class ProbePair:
    source: str
    destination: str
    destination_name: str | None = None
    source_leaf: str | None = None
    destination_leaf: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def source_attachment(self) -> str | None:
        """Neutral alias for the legacy Pingmesh ``source_leaf`` field."""
        return self.source_leaf

    @property
    def destination_attachment(self) -> str | None:
        """Neutral alias for the legacy Pingmesh ``destination_leaf`` field."""
        return self.destination_leaf


@dataclass(frozen=True)
class PingObservation:
    source: str
    destination: str
    requested_count: int
    sent: int
    received: int
    loss_rate: float
    payload_size: int | None
    dont_fragment: bool
    return_code: int
    rtt_samples_ms: tuple[float, ...] = ()
    rtt_min_ms: float | None = None
    rtt_avg_ms: float | None = None
    rtt_max_ms: float | None = None
    rtt_mdev_ms: float | None = None


@dataclass(frozen=True)
class ProbeOutcome:
    probe_id: str
    status: str
    evidence: tuple[Evidence, ...] = ()
    observations: tuple[Any, ...] = ()
    tool_calls: int = 0
    probe_packets: int = 0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    field: str


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class NormalizedResult:
    result: Any
    fault_type: FaultTypeNormalization
    interface: InterfaceNormalization
    validation_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteDecision:
    fast_path: bool
    family: str | None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RankedInterfaceCandidate:
    link_id: str
    primary_device: str
    primary_interface: str
    peer_device: str
    peer_interface: str
    score: float
    layer: str = "fabric"
    endpoint_confidence: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiagnosabilityDecision:
    can_submit: bool
    reason: str
    top_hypothesis_id: str | None = None
    confidence: float = 0.0
    margin: float = 0.0
    missing_requirements: tuple[str, ...] = ()


__all__ = [
    "BaseAgentAssessment",
    "BaseAgentStatus",
    "DiagnosisView",
    "FaultFamilyHint",
    "DiagnosticState",
    "Evidence",
    "EvidenceDirection",
    "EvidenceOrigin",
    "FaultTypeNormalization",
    "Hypothesis",
    "InterfaceNormalization",
    "NormalizedResult",
    "PingObservation",
    "ProbeOutcome",
    "ProbePair",
    "RankedInterfaceCandidate",
    "RouteDecision",
    "DiagnosabilityDecision",
    "ValidationIssue",
    "ValidationReport",
]
