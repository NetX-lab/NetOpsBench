"""Deterministic selective escalation for ambiguous diagnostic results."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from netopsbench.agents.base import VALID_AGENT_VERDICTS
from netopsbench.evaluator.fault_type_judge import supported_fault_types
from netopsbench.sdk.agents import DiagnosisResult

from ..config import NormalizationConfig, RouterConfig
from ..evidence.semantic import has_route_policy_evidence, normalize_next_hop
from ..evidence.store import EvidenceStore
from ..evidence.temporal import (
    is_repeated_bgp_state_sequence,
    is_repeated_flap_sequence,
    is_temporal_signal,
    signal_from_value,
)
from ..evidence.validator import can_plan_from, can_support_fault
from ..models import (
    BaseAgentAssessment,
    BaseAgentStatus,
    DiagnosisView,
    DiagnosticState,
    EvidenceOrigin,
    FaultFamilyHint,
    RouteDecision,
)
from ..topology.semantics import attachment_from_metadata
from ..verification.base_reliability import BaseAgentReliability
from .symptom_profile import build_symptom_profile

_IMPAIRMENT_FAMILIES = {
    "packet_loss": "packet_loss",
    "packet_corruption": "packet_corruption",
    "mtu_mismatch": "mtu",
    "high_latency": "high_latency",
}
_SEMANTIC_FAMILIES = {
    "acl_misconfig": "acl",
    "route_policy_misconfig": "route_policy",
    "static_route_misconfig": "static_route",
}
_OPERATIONAL_FAMILIES = {
    "bgp_neighbor_misconfig": "bgp_verification",
    "device_down": "link_state_verification",
    "link_down": "link_state_verification",
    "link_flapping": "link_flapping",
}
_SEMANTIC_FAULT_TYPES = {
    "acl": {"acl_misconfig"},
    "route_policy": {"route_policy_misconfig"},
    "static_route": {"static_route_misconfig", "blackhole_route"},
}
_GENERIC_LABELS = frozenset(
    {
        "connectivity_issue",
        "network_impairment",
        "packet_impairment",
        "performance_degradation",
        "unknown",
    }
)


def _result_text(result: DiagnosisResult) -> str:
    view = DiagnosisView.from_result(result)
    return " ".join((*view.evidence, result.reasoning or ""))


def _weak_family_from_text(text: str) -> str | None:
    lowered = " ".join(text.lower().split())
    patterns = (
        ("packet_loss", r"\b(?:sparse|intermittent|measured|observed|persistent)\b.{0,40}\b(?:packet )?loss\b"),
        ("high_latency", r"\b(?:elevated|high|abnormal|increased)\b.{0,30}\b(?:rtt|latency)\b"),
        ("mtu", r"\b(?:large|larger)\b.{0,30}\b(?:packet|df probe)s?\b.{0,30}\b(?:fail|drop)"),
        (
            "mtu",
            r"\bmtu\b.{0,40}\b\d{3,5}\b.{0,40}\b(?:vs\.?|versus|while|peer|mismatch|different|instead of)\b.{0,20}\b\d{3,5}\b",
        ),
        ("packet_corruption", r"\b(?:checksum invalid|payload mismatch|payload corruption|corrupt payload)\b"),
    )
    for family, pattern in patterns:
        match = re.search(pattern, lowered)
        if match is None:
            continue
        window = lowered[max(0, match.start() - 32) : min(len(lowered), match.end() + 32)]
        if re.search(
            r"\b(?:no|without|zero)\b.{0,24}\b(?:packet )?loss\b"
            r"|\b0(?:\.0+)?%\s+(?:packet )?loss\b"
            r"|\b(?:latency|rtt)\b.{0,16}\b(?:normal|healthy)\b",
            window,
        ):
            continue
        return family
    return None


def _policy_conflicts_with_bgp_label(fault_type: str | None, text: str) -> bool:
    if fault_type != "bgp_neighbor_misconfig":
        return False
    lowered = " ".join(text.lower().split())
    policy_signal = any(
        term in lowered
        for term in ("route policy", "route-policy", "route-map", "prefix filter", "network statement", "origination")
    )
    healthy_bgp = bool(
        re.search(r"\b(?:all )?bgp (?:sessions|neighbors|peers).{0,30}\b(?:established|healthy|up)\b", lowered)
    )
    return policy_signal and healthy_bgp


def _direct_route_policy_evidence(text: str) -> bool:
    """Require a concrete policy/config observation, not a speculative label."""
    return has_route_policy_evidence(" ".join(text.lower().split()))


def _runtime_failure(text: str, result: DiagnosisResult) -> bool:
    if result.verdict != "inconclusive" or result.confidence > 0:
        return False
    return bool(
        re.search(
            r"\b(?:recursion limit|graph_recursion_limit|timed? out|timeout|provider error|"
            r"api error|connection error)\b",
            text,
            re.IGNORECASE,
        )
    )


def _static_route_family_from_text(text: str) -> str | None:
    """Keep the legacy route fallback without re-parsing unrelated families."""
    lowered = " ".join(text.lower().split())
    if re.search(
        r"\bstatic route\b.{0,120}\b(?:missing|unresolv|invalid|incorrect|blackhole|no nexthops?|null0|discard)",
        lowered,
    ):
        return "static_route"
    return None


def _specific_hint_family(hints: Sequence[FaultFamilyHint]) -> str | None:
    for hint in hints:
        if hint.reliability <= 0.5:
            continue
        if hint.family in _SEMANTIC_FAMILIES.values():
            return hint.family
    return None


def _store_has_blackhole_route(store: EvidenceStore | None) -> bool:
    if store is None:
        return False
    for item in store.all():
        if item.reliability <= 0 or item.category not in {
            "configured_static_route",
            "unexpected_next_hop",
            "observed_routing_entry",
        }:
            continue
        value = item.value if isinstance(item.value, Mapping) else {}
        hop = value.get("next_hop") or value.get("configured_next_hop") or value.get("observed_next_hop")
        if normalize_next_hop(hop) in {"null0", "blackhole", "discard"}:
            return True
    return False


def _has_link_state_route_consequence(text: str) -> bool:
    """Recognize a public route consequence that usually means a link is down.

    A missing connected route is different from a selected Null0/blackhole
    route.  Keeping this distinction lets the router recover link-down cases
    where the base agent described the consequence as a blackhole without
    treating genuine discard routes as link failures.
    """
    normalized = " ".join(text.lower().split())
    cidr = r"(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}"
    has_network_object = bool(
        re.search(
            rf"\b(?:network|prefix|subnet)\s+{cidr}\b|\b{cidr}\b.{{0,24}}\b(?:network|prefix|subnet)\b",
            normalized,
        )
    )
    missing_connected = bool(
        re.search(
            r"\b(?:no|missing|without|absent)\s+(?:matching\s+)?(?:connected\s+)?route\b|"
            r"\b(?:connected\s+route|route)\b.{0,50}\b(?:not installed|not present|inaccessible|no best path)\b",
            normalized,
        )
    )
    missing_interface = bool(
        re.search(
            r"\b(?:no|missing|without|absent)\s+(?:matching\s+)?connected\s+interface\b"
            r"|\bsubnet\b.{0,100}\b(?:no|missing|without|absent)\s+(?:matching\s+)?connected\s+interface\b"
            r"|\bconnected\s+(?:subnet|interface)\b.{0,80}\b(?:gap|down|missing|absent)\b",
            normalized,
        )
    )
    return has_network_object and (missing_connected or missing_interface)


class HardCaseRouter:
    def __init__(
        self,
        config: RouterConfig | None = None,
        normalization: NormalizationConfig | None = None,
    ):
        self.config = config or RouterConfig()
        self.normalization = normalization or NormalizationConfig()
        self.canonical_fault_types = frozenset(supported_fault_types())
        self._reliability = BaseAgentReliability()

    def route(
        self,
        *,
        result: DiagnosisResult,
        state: DiagnosticState | None = None,
        evidence_store: EvidenceStore | None = None,
        normalization_errors: Sequence[str] = (),
        base_assessment: BaseAgentAssessment | None = None,
        semantic_family_hints: Sequence[FaultFamilyHint] = (),
    ) -> RouteDecision:
        view = DiagnosisView.from_result(result)
        fault_type, device, interface = view.fault_type, view.device, view.interface
        result_evidence = list(view.evidence)
        text = " ".join((*result_evidence, result.reasoning or ""))
        reasons: list[str] = []
        assessment = base_assessment or self._reliability.assess(
            result,
            evidence_store=evidence_store,
            normalization_errors=normalization_errors,
        )
        reliable_base = assessment.status == BaseAgentStatus.VALID
        family = None
        if reliable_base:
            family = (
                _IMPAIRMENT_FAMILIES.get(str(fault_type))
                or _SEMANTIC_FAMILIES.get(str(fault_type))
                or _OPERATIONAL_FAMILIES.get(str(fault_type))
            )
        direct_operational_family = _OPERATIONAL_FAMILIES.get(str(fault_type)) if reliable_base else None

        if not reliable_base:
            reasons.append(f"base_status_{assessment.status.value}")
        if not assessment.direct_evidence:
            reasons.append("missing_required_direct_evidence")
        if assessment.semantic_conflict:
            reasons.append("semantic_conflict")

        if normalization_errors:
            reasons.append("normalization_or_schema_error")
        if result.verdict not in VALID_AGENT_VERDICTS:
            reasons.append("invalid_verdict")
        if result.verdict == "fault_detected" and fault_type not in self.canonical_fault_types:
            reasons.append("noncanonical_fault_type")
        if result.verdict == "network_healthy" and any((fault_type, device, interface)):
            reasons.append("healthy_verdict_with_fault_location")
        if reliable_base and fault_type in _IMPAIRMENT_FAMILIES:
            reasons.append("impairment_fault_family")
        if reliable_base and fault_type in _GENERIC_LABELS:
            reasons.append("generic_impairment_label")
            family = family or _weak_family_from_text(text)
        if result.verdict == "fault_detected" and not device:
            reasons.append("missing_device")
        if (
            self.config.escalate_missing_interface
            and fault_type in self.normalization.interface_required_fault_types
            and not interface
        ):
            reasons.append("missing_required_interface")
            if reliable_base and fault_type in {"link_down", "device_down"}:
                family = "link_state_verification"

        if reliable_base and _policy_conflicts_with_bgp_label(fault_type, text):
            reasons.append("bgp_label_conflicts_with_route_policy_evidence")
            family = "route_policy"

        if reliable_base and fault_type == "blackhole_route" and _has_link_state_route_consequence(text):
            reasons.append("route_state_conflicts_with_blackhole_label")
            family = "link_state_verification"

        structured_family = self._family_from_store(evidence_store)
        direct_impairment_family = self._direct_impairment_family(
            evidence_store,
            allow_planning_only=not reliable_base,
        )
        specific_hint_family = _specific_hint_family(semantic_family_hints)
        concentrated_outage = self._concentrated_outage_device(evidence_store)
        observed_interface_down = self._has_down_interface(evidence_store)
        if observed_interface_down:
            if family != "link_state_verification":
                reasons.append("direct_interface_state_overrides_base_family")
            family = "link_state_verification"
        elif concentrated_outage:
            # A many-peer, near-total outage is first an operational-scope
            # question.  Link-state verification can still transition to a
            # semantic route check or packet-loss isolation when interfaces
            # remain healthy; treating it as random loss up front wastes the
            # active-probe budget and misses device-wide failures.
            reasons.append("concentrated_outage_requires_device_scope_triage")
            family = "link_state_verification"
        # Specific semantic observations outrank generic impairment counts,
        # while still forcing a hard path whenever they disagree with the base
        # label.  This prevents a handful of loss rows from masking a direct
        # route-policy/static-route observation.
        semantic_family = specific_hint_family or structured_family
        if self._has_direct_bgp_configuration_fault(evidence_store):
            if family != "bgp_verification":
                reasons.append("direct_bgp_configuration_overrides_base_family")
            family = "bgp_verification"
        elif semantic_family in _SEMANTIC_FAMILIES.values() and direct_operational_family is None:
            if family is not None and family != semantic_family:
                reasons.append("specific_semantic_family_mismatch")
            family = semantic_family
        elif semantic_family in _SEMANTIC_FAMILIES.values() and semantic_family != direct_operational_family:
            # An explicit observed link state/transition is more proximal than
            # its downstream route consequence.  Keep the operational family
            # and force validation rather than relabelling the result.
            reasons.append("semantic_hint_is_downstream_of_operational_fault")
            family = direct_operational_family
        elif reliable_base and structured_family is not None:
            family = family or structured_family
        if semantic_family == "static_route" and _store_has_blackhole_route(evidence_store):
            if fault_type != "blackhole_route":
                reasons.append("blackhole_route_semantic_mismatch")
            family = "static_route"
        if (
            direct_impairment_family is not None
            and family not in _SEMANTIC_FAMILIES.values()
            and not self._has_down_interface(evidence_store)
            and not concentrated_outage
        ):
            if family != direct_impairment_family:
                reasons.append(
                    "direct_impairment_observation_overrides_base_family"
                    if family is not None
                    else "direct_impairment_observation_selected_family"
                )
            family = direct_impairment_family
        repeated_temporal = self._has_repeated_temporal_signature(evidence_store, result)
        if (
            self._has_temporal_signature(evidence_store, result)
            and ((direct_operational_family is None and not observed_interface_down) or repeated_temporal)
            and semantic_family not in _SEMANTIC_FAMILIES.values()
            and (direct_impairment_family is None or structured_family == "link_flapping")
        ):
            # A recovered interface/BGP session looks healthy at the instant
            # of diagnosis.  Timestamped transitions are nevertheless direct
            # temporal observations and must veto a healthy fast path for any
            # provider, including one whose final prose says "recovered".
            reasons.append("structured_temporal_transition_requires_verification")
            family = "temporal_verification"
        if evidence_store is not None and evidence_store.conflicts:
            reasons.append("conflicting_evidence")

        # Free-form prose is a last-resort planning hint.  Once public
        # structured observations are available, prose alone must not turn a
        # healthy result into an impairment branch.  This avoids provider-
        # specific adjectives (for example "elevated" for a healthy 7 ms
        # tail) controlling the harness.
        weak_text_family = _weak_family_from_text(text) if evidence_store is None else None
        if (
            result.verdict == "network_healthy"
            and self.config.escalate_healthy_with_weak_symptom
            and (structured_family or weak_text_family)
        ):
            reasons.append("healthy_verdict_with_performance_symptom")
            symptom_family = structured_family or weak_text_family
            # A weak aggregate loss row is a reason to verify health, not to
            # assume random packet loss. Direct/repeated loss observations can
            # still select the impairment branch through the normal profile.
            family = (
                "healthy_verification"
                if symptom_family == "packet_loss" and not self._has_material_loss(evidence_store)
                else symptom_family
            )
        if structured_family == "packet_loss" and self._has_destination_concentrated_partial_loss(evidence_store):
            reasons.append("concentrated_loss_requires_route_semantic_contrast")
            family = "runtime_semantic"

        if (
            result.verdict == "fault_detected"
            and fault_type == "route_policy_misconfig"
            and structured_family == "packet_loss"
            and specific_hint_family != "route_policy"
            and not _direct_route_policy_evidence(text)
        ):
            reasons.append("semantic_label_conflicts_with_structured_impairment")
            family = "packet_loss"

        if _runtime_failure(text, result):
            reasons.append("base_runtime_failure")
        if not reliable_base:
            hinted = next((hint.family for hint in semantic_family_hints if hint.reliability > 0.5), None)
            # Provider prose is not a routing authority. Specific semantic
            # claims have already passed the negation-aware structured hint
            # extractor; falling back to a second regex parser here caused
            # phrases such as "no ACL blocks" to enter the ACL branch.
            observed_semantic_family = structured_family if structured_family in _SEMANTIC_FAMILIES.values() else None
            semantic_family = hinted or observed_semantic_family or _static_route_family_from_text(text)
            if semantic_family == "link_flapping":
                semantic_family = "temporal_verification"
            # An unreliable base label is never submission evidence, but a
            # specific operational claim with a concrete device remains a
            # useful bounded verification plan.  Verifying link/device state
            # first is cheaper and safer than spending the impairment budget
            # on downstream loss symptoms.  A clean operational check still
            # falls through to the structured impairment family.
            operational_hint = _OPERATIONAL_FAMILIES.get(str(fault_type)) if device else None
            if operational_hint == "link_flapping":
                operational_hint = "temporal_verification"
            impairment_hint = _weak_family_from_text(text)
            inferred = (
                ("bgp_verification" if self._has_direct_bgp_configuration_fault(evidence_store) else None)
                or semantic_family
                or direct_impairment_family
                or (impairment_hint if impairment_hint in {"mtu", "high_latency", "packet_corruption"} else None)
                or operational_hint
                or self._unreliable_family(result, evidence_store)
            )
            if family not in {
                "healthy_verification",
                "link_state_verification",
                "temporal_verification",
            } or (family == "temporal_verification" and operational_hint and not repeated_temporal):
                family = inferred

        if self._has_close_hypotheses(state):
            reasons.append("close_hypothesis_scores")

        if result.confidence < self.config.fast_path_confidence:
            reasons.append("confidence_below_fast_path_threshold")
        if not result_evidence:
            reasons.append("no_direct_evidence")

        # A fast path is only valid when its selected family is compatible
        # with the canonical base result.  The family itself is routing
        # metadata, not a submission label, so a mismatch must be escalated.
        if family in _SEMANTIC_FAULT_TYPES and fault_type not in _SEMANTIC_FAULT_TYPES[family]:
            reasons.append("family_incompatible_with_base_fault_type")
        if (
            family == "static_route"
            and fault_type == "blackhole_route"
            and not _store_has_blackhole_route(evidence_store)
        ):
            # Do not trust a blackhole label without a selected route object.
            reasons.append("blackhole_without_route_evidence")

        return RouteDecision(fast_path=not reasons, family=family, reasons=tuple(dict.fromkeys(reasons)))

    def _unreliable_family(
        self,
        result: DiagnosisResult,
        evidence_store: EvidenceStore | None,
    ) -> str:
        """Route by public observations when the base result is not authoritative."""
        if self._has_down_interface(evidence_store):
            return "link_state_verification"
        if self._has_direct_bgp_fault(evidence_store):
            return "bgp_verification"
        if _has_link_state_route_consequence(_result_text(result)):
            return "link_state_verification"
        family = self._family_from_store(evidence_store)
        if family in {"mtu", "high_latency", "packet_corruption"}:
            return family
        if family == "link_flapping":
            return "temporal_verification"
        if self._has_temporal_signature(evidence_store, result):
            return "temporal_verification"
        losses = self._loss_values(evidence_store)
        if losses:
            if max(losses) >= 0.80:
                if _runtime_failure(_result_text(result), result):
                    return "runtime_semantic"
                # A concentrated outage first needs direct interface-state
                # verification; it must not be labelled as random loss.
                return "link_state_verification"
            if max(losses) < 0.10:
                # Low-rate transient loss is ambiguous until temporal BGP/log
                # observations distinguish flapping from random loss.
                return "temporal_verification"
            if self._has_destination_concentrated_partial_loss(evidence_store):
                # A many-source/one-destination ECMP pattern can be produced
                # by a selected discard route on one fabric member. Inspect a
                # bounded route contrast first; if it is clean the orchestrator
                # falls back to the normal packet-loss discriminator.
                return "runtime_semantic"
            return "packet_loss"
        if result.verdict == "network_healthy" or (
            _runtime_failure(_result_text(result), result) and not (result.findings or {}).get("fault_type")
        ):
            return "healthy_verification"
        return "generic_verification"

    @staticmethod
    def _has_down_interface(evidence_store: EvidenceStore | None) -> bool:
        if evidence_store is None:
            return False
        for item in evidence_store.all():
            if item.category not in {"interface_admin_state", "interface_oper_state"} or not can_support_fault(item):
                continue
            if str(item.value).lower() in {"down", "false", "0", "x"} or item.value is False:
                return True
        return False

    @staticmethod
    def _has_direct_bgp_fault(evidence_store: EvidenceStore | None) -> bool:
        return HardCaseRouter._has_direct_bgp_marker(evidence_store, "direct_bgp_evidence")

    @staticmethod
    def _has_direct_bgp_configuration_fault(evidence_store: EvidenceStore | None) -> bool:
        return HardCaseRouter._has_direct_bgp_marker(evidence_store, "direct_bgp_configuration_evidence")

    @staticmethod
    def _has_direct_bgp_marker(evidence_store: EvidenceStore | None, marker: str) -> bool:
        if evidence_store is None:
            return False
        return any(
            item.category == "bgp_neighbor_state"
            and can_support_fault(item)
            and bool(item.metadata.get(marker))
            for item in evidence_store.all()
        )

    @staticmethod
    def _direct_impairment_family(
        evidence_store: EvidenceStore | None,
        *,
        allow_planning_only: bool = False,
    ) -> str | None:
        """Choose only discriminators backed by concrete non-prose data."""
        if evidence_store is None:
            return None
        candidates: list[tuple[int, str]] = []
        for item in evidence_store.all():
            if item.origin is EvidenceOrigin.BASE_CLAIM or item.reliability <= 0:
                continue
            if item.category == "payload_integrity_failure" and can_support_fault(item) and bool(item.value):
                candidates.append((4, "packet_corruption"))
            elif item.category == "packet_size_threshold" and (
                can_support_fault(item) or (allow_planning_only and can_plan_from(item))
            ):
                value = item.value if isinstance(item.value, Mapping) else {}
                if value.get("size_dependent_failure"):
                    # A Pingmesh DF/ordinary-probe contrast may be planning
                    # only until an active sweep finds the exact threshold.
                    # It is still the most specific bounded next action only
                    # when the base result is unreliable; a valid base result
                    # cannot be overridden by a planning-only observation.
                    # The final MTU contract continues to require threshold
                    # and configuration proof.
                    candidates.append((3, "mtu"))
            elif (
                item.category == "configuration_difference"
                and str(item.metadata.get("semantic_family") or "") in {"mtu", "mtu_mismatch"}
                and can_plan_from(item)
            ):
                candidates.append((3, "mtu"))
            elif (
                item.category in {"latency_median", "latency_p95"}
                and can_support_fault(item)
                and item.path_observation_confidence >= 1.0
                and item.covered_links
                and any(
                    item.metadata.get(marker) for marker in ("category_anomaly", "absolute_anomaly", "relative_anomaly")
                )
            ):
                candidates.append((2, "high_latency"))
        return max(candidates, default=(0, None))[1]

    @staticmethod
    def _loss_values(evidence_store: EvidenceStore | None) -> list[float]:
        values: list[float] = []
        if evidence_store is None:
            return values
        for item in evidence_store.all():
            if item.category != "packet_loss_rate" or not can_support_fault(item):
                continue
            try:
                value = float(item.value)
            except (TypeError, ValueError):
                continue
            if value > 0:
                values.append(value)
        return values

    def _has_material_loss(self, evidence_store: EvidenceStore | None) -> bool:
        return any(value >= self.config.direct_loss_routing_threshold for value in self._loss_values(evidence_store))

    @staticmethod
    def _has_destination_concentrated_partial_loss(evidence_store: EvidenceStore | None) -> bool:
        if evidence_store is None:
            return False
        destinations: dict[str, set[str]] = {}
        sources: dict[str, set[str]] = {}
        for item in evidence_store.all():
            if item.category != "packet_loss_rate" or not can_support_fault(item):
                continue
            try:
                loss = float(item.value)
            except (TypeError, ValueError):
                continue
            if not 0.10 <= loss < 0.80:
                continue
            src_ip = str(item.metadata.get("src_ip") or "")
            dst_ip = str(item.metadata.get("dst_ip") or "")
            source_attachment = attachment_from_metadata(item.metadata, "source") or ""
            destination_attachment = attachment_from_metadata(item.metadata, "destination") or ""
            if dst_ip and source_attachment:
                destinations.setdefault(dst_ip, set()).add(source_attachment)
            if src_ip and destination_attachment:
                sources.setdefault(src_ip, set()).add(destination_attachment)
        return max((len(peers) for peers in (*destinations.values(), *sources.values())), default=0) >= 3

    def observable_family(self, evidence_store: EvidenceStore | None) -> str | None:
        """Return a fault family supported by public structured observations.

        This is intentionally narrower than ``route``: it is used only after a
        base-runtime failure and an unsuccessful semantic closure, so runtime
        error text cannot override the observation-derived family again.
        """
        scores = build_symptom_profile(evidence_store, self.config).family_scores
        observable = {
            family: score
            for family, score in scores.items()
            if family in {"packet_loss", "packet_corruption", "mtu", "high_latency"}
        }
        return max(observable, key=lambda family: (observable[family], family), default=None)

    @staticmethod
    def has_partial_packet_loss(evidence_store: EvidenceStore | None) -> bool:
        """Whether public observations show degradation rather than outage."""
        if evidence_store is None:
            return False
        for item in evidence_store.all():
            if item.category != "packet_loss_rate" or not can_support_fault(item):
                continue
            try:
                loss_rate = float(item.value)
            except (TypeError, ValueError):
                continue
            if 0.0 < loss_rate < 0.8:
                return True
        return False

    def _family_from_store(self, store: EvidenceStore | None) -> str | None:
        return build_symptom_profile(store, self.config).primary_family

    @staticmethod
    def _concentrated_outage_device(evidence_store: EvidenceStore | None) -> str | None:
        """Return an attachment switch shared by independent outage paths."""
        if evidence_store is None:
            return None
        support: dict[str, set[str]] = {}
        rows: dict[str, int] = {}
        for item in evidence_store.all():
            if item.category != "packet_loss_rate" or not can_support_fault(item):
                continue
            try:
                if float(item.value) < 0.80:
                    continue
            except (TypeError, ValueError):
                continue
            source = attachment_from_metadata(item.metadata, "source") or ""
            destination = attachment_from_metadata(item.metadata, "destination") or ""
            if not source or not destination or source == destination:
                continue
            for device, peer in ((source, destination), (destination, source)):
                support.setdefault(device, set()).add(peer)
                rows[device] = rows.get(device, 0) + 1
        candidates = [device for device in support if len(support[device]) >= 2 and rows.get(device, 0) >= 3]
        return max(candidates, key=lambda device: (rows[device], len(support[device]), device), default=None)

    @staticmethod
    def _has_temporal_signature(store: EvidenceStore | None, result: DiagnosisResult) -> bool:
        text = _result_text(result).lower()
        for match in re.finditer(
            r"\b(?:flap|flapping|up/down|down/up|transition|session reset|state changes?)\b", text
        ):
            prefix = text[max(0, match.start() - 32) : match.start()]
            if not re.search(r"\b(?:no|not|without|never)\b[^.;]{0,24}$", prefix):
                return True
        if store is None:
            return False
        loss_count = 0
        early_only_count = 0
        for item in store.all():
            if item.category == "syslog_event" and can_support_fault(item):
                signal = signal_from_value(item.value)
                if is_temporal_signal(signal):
                    return True
                if signal is None and isinstance(item.value, Mapping) and item.value.get("temporal_transition"):
                    # Older structured emitters may not have retained their
                    # raw message. Their explicit transition flag remains a
                    # valid planning signal, but cannot prove a repeated flap.
                    return True
            if item.category == "bgp_neighbor_state" and can_plan_from(item):
                value = item.value if isinstance(item.value, Mapping) else {}
                states = value.get("states_observed")
                if (
                    bool(value.get("temporal_transition"))
                    or str(value.get("event_type") or "").lower() in {"session_flap", "state_transition"}
                    or (
                        isinstance(states, Sequence)
                        and not isinstance(states, (str, bytes))
                        and len({str(state).upper() for state in states}) > 1
                    )
                ):
                    return True
            if item.category != "packet_loss_rate" or not can_support_fault(item):
                continue
            loss_count += 1
            persistence = str(item.metadata.get("persistence") or "").lower()
            if persistence in {"intermittent", "transient", "flapping"}:
                return True
            if persistence == "early_only":
                early_only_count += 1
        # A single early-only row can occur as sampling noise in an otherwise
        # persistent loss event.  Require a repeated temporal pattern before
        # spending the branch budget on BGP/log verification.
        return early_only_count >= 2 and early_only_count / max(1, loss_count) >= 0.20

    @staticmethod
    def _has_repeated_temporal_signature(store: EvidenceStore | None, result: DiagnosisResult) -> bool:
        """Distinguish a recovered flap sequence from one outage transition."""
        if store is not None:
            groups: dict[str, list[str | None]] = {}
            for item in store.all():
                if item.category == "syslog_event" and can_support_fault(item):
                    signal = signal_from_value(item.value)
                    if not is_temporal_signal(signal):
                        continue
                    link = str(item.metadata.get("link_id") or item.entity_id)
                    groups.setdefault(link, []).append(signal)
                elif item.category == "bgp_neighbor_state" and can_plan_from(item):
                    value = item.value if isinstance(item.value, Mapping) else {}
                    if str(item.metadata.get("event_type") or value.get("event_type") or "").lower() == "session_flap":
                        return True
                    states = value.get("states_observed")
                    if is_repeated_bgp_state_sequence(states):
                        return True
            if any(is_repeated_flap_sequence(events) for events in groups.values()):
                return True
            return False
        text = _result_text(result).lower()
        return bool(re.search(r"\b(?:twice|multiple|repeated)\b.{0,40}\b(?:flap|transition|down/up|up/down)", text))

    def _has_close_hypotheses(self, state: DiagnosticState | None) -> bool:
        if state is None or len(state.hypotheses) < 2:
            return False
        ranked = sorted(
            (
                hypothesis.probability if hypothesis.probability > 0 else hypothesis.score
                for hypothesis in state.hypotheses.values()
            ),
            reverse=True,
        )
        return ranked[0] - ranked[1] < self.config.close_hypothesis_margin


__all__ = ["HardCaseRouter"]
