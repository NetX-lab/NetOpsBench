"""Observation-first symptom aggregation for hard-case routing.

This module selects a *verification family*.  Its scores are not diagnosis
confidence and cannot bypass evidence contracts or the diagnosability gate.
Keeping the aggregation pure makes initial routing and bounded replanning use
the same rules across topology sizes and base-agent providers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ..config import RouterConfig
from ..evidence.store import EvidenceStore
from ..evidence.validator import can_plan_from, can_support_fault
from ..models import Evidence, EvidenceOrigin

_CATEGORY_FAMILIES = {
    "packet_loss_rate": "packet_loss",
    "packet_size_threshold": "mtu",
    "payload_integrity_failure": "packet_corruption",
    "latency_median": "high_latency",
    "latency_p95": "high_latency",
    "syslog_event": "link_flapping",
}
_SEMANTIC_FAMILIES = frozenset({"acl", "route_policy", "static_route"})
_TRANSIENT_LOSS_STATES = frozenset({"early_only", "intermittent", "transient", "flapping"})


@dataclass(frozen=True)
class SymptomProfile:
    """Compact, provider-neutral summary of current structured observations."""

    family_scores: dict[str, float] = field(default_factory=dict)
    family_evidence_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def ranked_families(self) -> tuple[str, ...]:
        return tuple(sorted(self.family_scores, key=lambda family: (-self.family_scores[family], family)))

    @property
    def primary_family(self) -> str | None:
        ranked = self.ranked_families
        return ranked[0] if ranked else None

    @property
    def margin(self) -> float:
        ranked = self.ranked_families
        if not ranked:
            return 0.0
        if len(ranked) == 1:
            return self.family_scores[ranked[0]]
        return self.family_scores[ranked[0]] - self.family_scores[ranked[1]]


def build_symptom_profile(
    store: EvidenceStore | None,
    config: RouterConfig | None = None,
) -> SymptomProfile:
    """Aggregate qualified observations without multiplying correlated rows."""

    if store is None:
        return SymptomProfile()
    policy = config or RouterConfig()
    contributions: dict[tuple[str, str], tuple[float, str]] = {}
    for item in store.all():
        for family, score in _item_contributions(item, policy):
            key = (family, item.independence_key or item.evidence_id)
            current = contributions.get(key)
            if current is None or score > current[0]:
                contributions[key] = (score, item.evidence_id)

    scores: dict[str, float] = {}
    evidence_ids: dict[str, list[str]] = {}
    for (family, _source), (score, evidence_id) in contributions.items():
        scores[family] = scores.get(family, 0.0) + score
        evidence_ids.setdefault(family, []).append(evidence_id)
    return SymptomProfile(
        family_scores=scores,
        family_evidence_ids={family: tuple(dict.fromkeys(ids)) for family, ids in evidence_ids.items()},
    )


def _item_contributions(item: Evidence, config: RouterConfig) -> tuple[tuple[str, float], ...]:
    # Base prose has a separate, explicitly weak semantic-hint path.  It must
    # not re-enter the observation profile and make a disproved family win a
    # second time during replanning.
    if item.origin is EvidenceOrigin.BASE_CLAIM:
        return ()
    weights = config.family_signal_weights
    contributions: list[tuple[str, float]] = []
    semantic_family = str(item.metadata.get("semantic_family") or "")
    if can_plan_from(item):
        if item.category == "configuration_difference" and semantic_family in _SEMANTIC_FAMILIES:
            contributions.append((semantic_family, weights["configuration_difference"] * item.reliability))
        elif item.category == "configuration_difference" and semantic_family in {"mtu", "mtu_mismatch"}:
            # This only selects a verification family. Submission still needs
            # a size threshold and peer/config mismatch.
            contributions.append(("mtu", weights["configuration_difference"] * item.reliability))
        elif item.category in {"configured_static_route", "observed_routing_entry"} and (
            semantic_family == "static_route" or _is_static_route(item)
        ):
            contributions.append(("static_route", weights[item.category] * item.reliability))

    if item.category not in _CATEGORY_FAMILIES or not (
        can_support_fault(item) or (item.category == "packet_size_threshold" and can_plan_from(item))
    ):
        return tuple(contributions)
    if not _qualified(item, config):
        return tuple(contributions)

    family = _CATEGORY_FAMILIES[item.category]
    score = weights[item.category] * item.reliability
    if item.category == "packet_size_threshold" and not can_support_fault(item):
        # Pingmesh can identify the useful triage signature "ordinary RTT
        # probes pass while DF probes fail", but it cannot prove a stable MTU
        # threshold.  Keep that signature strong enough to select an MTU
        # sweep when it is the only symptom, while ensuring independently
        # observed ordinary packet loss wins when both are present.  The
        # active size sweep remains full-weight and is the only form accepted
        # by the MTU evidence contract.
        score *= 0.25
    if item.category == "packet_loss_rate" and item.metadata.get("weak_performance_symptom"):
        score *= 0.5
    if item.path_observation_confidence >= 1.0:
        score *= 1.25
    contributions.append((family, score))
    if item.category == "packet_loss_rate" and str(item.metadata.get("persistence") or "").lower() in (
        _TRANSIENT_LOSS_STATES
    ):
        contributions.append(("link_flapping", weights["transient_loss"] * item.reliability))
    return tuple(contributions)


def _qualified(item: Evidence, config: RouterConfig) -> bool:
    if item.category == "packet_loss_rate":
        try:
            loss_rate = float(item.value)
        except (TypeError, ValueError):
            return False
        return loss_rate >= config.direct_loss_routing_threshold or bool(
            item.metadata.get("aggregate_pingmesh")
            and item.metadata.get("weak_performance_symptom")
            and loss_rate >= config.weak_loss_routing_threshold
        )
    if item.category == "packet_size_threshold":
        value = item.value if isinstance(item.value, Mapping) else {}
        return bool(value.get("size_dependent_failure"))
    if item.category == "payload_integrity_failure":
        return bool(item.value)
    if item.category in {"latency_median", "latency_p95"}:
        if any(item.metadata.get(marker) for marker in ("category_anomaly", "absolute_anomaly", "relative_anomaly")):
            return True
        try:
            return float(item.value) >= config.latency_routing_threshold_ms
        except (TypeError, ValueError):
            return False
    return True


def _is_static_route(item: Evidence) -> bool:
    return isinstance(item.value, Mapping) and str(item.value.get("protocol") or "").lower() == "static"


__all__ = ["SymptomProfile", "build_symptom_profile"]
