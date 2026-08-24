"""Contrast abnormal and healthy path evidence without assuming one ECMP path."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any

from ..config import TopologyRankerConfig
from ..evidence.validator import can_plan_from
from ..models import Evidence
from .graph import TopologyGraph
from .semantics import attachment_from_metadata


@dataclass
class PathEvidenceAnalysis:
    abnormal_support: dict[str, float] = field(default_factory=dict)
    healthy_support: dict[str, float] = field(default_factory=dict)
    active_probe_support: dict[str, float] = field(default_factory=dict)
    counter_support: dict[str, float] = field(default_factory=dict)
    configuration_support: dict[str, float] = field(default_factory=dict)
    evidence_ids: dict[str, set[str]] = field(default_factory=dict)
    # Keep causal endpoint attribution separate from endpoint-local symptoms.
    # A directional one-hop probe or a concrete configuration difference may
    # identify the fault-owning endpoint.  RX discards and similar counters
    # often identify only where a consequence was observed; repeated reads of
    # that counter must not outvote direct causal evidence.
    causal_endpoint_support: dict[tuple[str, str], float] = field(default_factory=dict)
    symptom_endpoint_support: dict[tuple[str, str], float] = field(default_factory=dict)
    endpoint_support: dict[tuple[str, str], float] = field(default_factory=dict)
    unresolved_paths: list[str] = field(default_factory=list)


def _path_endpoints(evidence: Evidence) -> tuple[str | None, str | None]:
    source = evidence.metadata.get("source") or evidence.metadata.get("src_name")
    destination = evidence.metadata.get("destination") or evidence.metadata.get("dst_name")
    if source and destination:
        return str(source), str(destination)
    if evidence.entity_type == "path" and "--" in evidence.entity_id:
        left, right = evidence.entity_id.split("--", 1)
        return left, right
    return None, None


def scope_path_evidence(graph: TopologyGraph, evidence: Evidence, *, max_paths: int = 32) -> Evidence:
    """Attach topology path scope without claiming which ECMP member was used."""
    if evidence.entity_type != "path":
        return evidence
    source, destination = _path_endpoints(evidence)
    if source is None or destination is None:
        return evidence
    profile = graph.shortest_path_profile(source, destination, max_paths=max_paths)
    possible_paths = tuple(tuple(path) for path in profile.paths)
    if not possible_paths:
        return evidence

    observed = evidence.observed_path
    if observed is None:
        raw_observed = evidence.metadata.get("observed_path")
        if isinstance(raw_observed, (list, tuple)) and raw_observed:
            observed = tuple(str(link_id) for link_id in raw_observed)
    # A single topology path is not an ECMP guess: every successful packet
    # between these endpoints necessarily covers it. This is particularly
    # useful for read-only one-hop fabric isolation probes.
    if observed is None and len(possible_paths) == 1:
        observed = possible_paths[0]
    covered = evidence.covered_links or (tuple(dict.fromkeys(observed)) if observed is not None else ())
    confidence = evidence.path_observation_confidence
    if observed is not None and confidence <= 0:
        confidence = 1.0
    metadata = dict(evidence.metadata)
    metadata.update(
        {
            "shortest_path_count": profile.total_paths,
            "paths_truncated": profile.truncated,
            "path_link_fractions": profile.link_fractions,
        }
    )
    return replace(
        evidence,
        observed_path=observed,
        possible_paths=possible_paths,
        covered_links=covered,
        path_observation_confidence=min(1.0, max(0.0, confidence)),
        metadata=metadata,
    )


def has_unresolved_ecmp_ambiguity(evidence_items: list[Evidence]) -> bool:
    return any(
        len(item.possible_paths) > 1 and item.observed_path is None and not item.covered_links
        for item in evidence_items
        if item.entity_type == "path" and item.reliability > 0
    )


def _path_state(
    evidence: Evidence,
    config: TopologyRankerConfig,
    *,
    family: str | None = None,
) -> str | None:
    if evidence.category in {"tool_error", "missing_observation"} or evidence.reliability <= 0:
        return None
    relevant_categories = {
        "packet_loss": {"packet_loss_rate", "payload_integrity_failure"},
        "packet_corruption": {"packet_loss_rate", "payload_integrity_failure"},
        "mtu": {"packet_size_threshold"},
        "mtu_mismatch": {"packet_size_threshold"},
        "high_latency": {"latency_median", "latency_p95"},
    }
    if family in relevant_categories and evidence.category not in relevant_categories[family]:
        # A successful loss probe does not refute latency, and a low RTT does
        # not refute corruption.  Path contrast is meaningful only within the
        # fault family whose observable the probe actually measured.
        return None
    if evidence.category == "packet_loss_rate":
        value = float(evidence.value or 0.0)
        if evidence.source == "ping_test_repeated" and 0.0 < value < max(config.abnormal_loss_threshold, 0.10):
            return None
        if value >= config.abnormal_loss_threshold:
            return "abnormal"
        if value <= config.healthy_loss_threshold:
            return "healthy"
    if evidence.category in {"latency_median", "latency_p95"}:
        if "category_anomaly" in evidence.metadata:
            if evidence.metadata.get("category_anomaly"):
                return "abnormal"
        elif evidence.metadata.get("absolute_anomaly") or evidence.metadata.get("relative_anomaly"):
            return "abnormal"
        value = float(evidence.value or 0.0)
        baseline = evidence.metadata.get("baseline")
        threshold = evidence.metadata.get("threshold")
        if threshold is not None and value >= float(threshold):
            return "abnormal"
        if value >= config.abnormal_latency_ms:
            return "abnormal"
        if baseline is not None and value <= max(float(baseline) * 2.0, 1.0):
            return "healthy"
        if evidence.probe_id and value < config.abnormal_latency_ms:
            return "healthy"
    if evidence.category == "packet_size_threshold" and isinstance(evidence.value, dict):
        return "abnormal" if evidence.value.get("size_dependent_failure") else "healthy"
    if evidence.category == "payload_integrity_failure":
        if bool(evidence.value):
            return "abnormal"
        # A valid checksum describes the packets that arrived.  It is not a
        # healthy-path observation when the same bounded invocation also saw
        # missing sequences; the paired packet_loss_rate evidence carries the
        # path anomaly in that case.
        if int(evidence.metadata.get("missing_packets") or 0) > 0:
            return None
        return "healthy"
    return None


def _normalize(values: dict[str, float]) -> dict[str, float]:
    # Preserve absolute support.  Max-normalizing a set makes an arbitrarily
    # weak relative outlier look like certainty on a larger topology.
    return {key: min(1.0, max(0.0, value)) for key, value in values.items()}


def analyze_path_evidence(
    graph: TopologyGraph,
    evidence_items: list[Evidence],
    *,
    config: TopologyRankerConfig,
    family: str | None = None,
) -> PathEvidenceAnalysis:
    abnormal: dict[str, float] = defaultdict(float)
    healthy: dict[str, float] = defaultdict(float)
    active: dict[str, float] = defaultdict(float)
    counters: dict[str, float] = defaultdict(float)
    differences: dict[str, float] = defaultdict(float)
    ids: dict[str, set[str]] = defaultdict(set)
    causal_endpoint_support: dict[tuple[str, str], float] = defaultdict(float)
    symptom_endpoint_support: dict[tuple[str, str], float] = defaultdict(float)
    unresolved: list[str] = []

    for evidence in evidence_items:
        # Planning-only public symptoms may identify the bounded path domain,
        # but they remain excluded from hypothesis scoring and submission.
        if not can_plan_from(evidence):
            continue
        if evidence.category in {"interface_counter_delta", "configuration_difference"}:
            if evidence.entity_type != "interface" or ":" not in evidence.entity_id:
                continue
            device, interface = evidence.entity_id.split(":", 1)
            link = graph.endpoint_link(device, interface)
            if link is None:
                continue
            value: Any = evidence.value
            abnormal_value = False
            if isinstance(value, dict):
                abnormal_value = any(
                    float(item or 0.0) != 0.0 for item in value.values() if isinstance(item, (int, float))
                )
                abnormal_value = abnormal_value or bool(value.get("different"))
            else:
                abnormal_value = bool(value)
            if not abnormal_value:
                continue
            target = counters if evidence.category == "interface_counter_delta" else differences
            target[link.link_id] += evidence.reliability
            ids[link.link_id].add(evidence.evidence_id)
            endpoint_key = (device, interface)
            endpoint_weight = evidence.reliability * 10.0
            if evidence.category == "configuration_difference":
                causal_endpoint_support[endpoint_key] = max(
                    causal_endpoint_support[endpoint_key],
                    endpoint_weight,
                )
            else:
                # Counter samples from the same interface/window are usually
                # correlated observations, not independent votes.  Retain the
                # strongest symptom instead of multiplying it by query count.
                symptom_endpoint_support[endpoint_key] = max(
                    symptom_endpoint_support[endpoint_key],
                    endpoint_weight,
                )
            continue

        state = _path_state(evidence, config, family=family)
        if state is None:
            continue
        source, destination = _path_endpoints(evidence)
        if source is None or destination is None:
            unresolved.append(evidence.evidence_id)
            continue
        exact_fractions = evidence.metadata.get("path_link_fractions")
        if evidence.observed_path is not None:
            paths = [list(evidence.observed_path)]
            fractions = {link_id: 1.0 for link_id in set(evidence.observed_path)}
        elif isinstance(exact_fractions, dict) and exact_fractions:
            paths = []
            fractions = {
                str(link_id): float(fraction)
                for link_id, fraction in exact_fractions.items()
                if graph.link(str(link_id)) is not None
            }
        elif evidence.possible_paths:
            paths = [list(path) for path in evidence.possible_paths]
            fractions = {}
        else:
            profile = graph.shortest_path_profile(source, destination, max_paths=config.max_ecmp_paths)
            paths = profile.paths
            fractions = profile.link_fractions
        if not paths and not fractions:
            unresolved.append(evidence.evidence_id)
            continue
        if not fractions:
            membership: dict[str, int] = defaultdict(int)
            for path in paths:
                for link_id in set(path):
                    membership[link_id] += 1
            fractions = {link_id: count / len(paths) for link_id, count in membership.items()}
        weight = evidence.reliability
        # A successful active sample with an unknown ECMP hash only establishes
        # that one unknown member was healthy. It must not contradict every
        # possible member. Confirmed observed paths remain valid negatives.
        if state == "healthy" and evidence.probe_id and evidence.observed_path is None:
            continue
        for link_id, path_fraction in fractions.items():
            fractional = weight * path_fraction
            (abnormal if state == "abnormal" else healthy)[link_id] += fractional
            if state == "abnormal" and evidence.probe_id:
                active[link_id] += fractional
            ids[link_id].add(evidence.evidence_id)
        if state == "abnormal":
            suspect_attachment = evidence.metadata.get("suspect_attachment") or evidence.metadata.get("suspect_leaf")
            covered_physical_links = [link_id for link_id in evidence.covered_links if graph.link(link_id) is not None]
            covered_network_links = [
                link_id
                for link_id in covered_physical_links
                if (link := graph.link(link_id)) is not None
                and graph.roles.get(link.physical.endpoint_a.device) != "client"
                and graph.roles.get(link.physical.endpoint_b.device) != "client"
            ]
            directly_bound_links = (
                covered_physical_links if evidence.metadata.get("fault_endpoint_device") else covered_network_links
            )
            direct_isolation = bool(
                evidence.metadata.get("selection")
                in {
                    "link_isolation",
                    "link_integrity",
                    "adaptive_ecmp_link_isolation",
                    "directional_link_latency",
                }
                and evidence.observed_path is not None
                and len(directly_bound_links) == 1
            )
            for side in ("source", "destination"):
                device = attachment_from_metadata(evidence.metadata, side)
                if not device:
                    continue
                for link_id in fractions:
                    link = graph.link(link_id)
                    if link is None:
                        continue
                    for endpoint in (link.physical.endpoint_a, link.physical.endpoint_b):
                        if endpoint.device == device:
                            key = (endpoint.device, endpoint.canonical_interface)
                            symptom_endpoint_support[key] = max(symptom_endpoint_support[key], weight)
            if direct_isolation:
                link = graph.link(directly_bound_links[0])
                if link is not None:
                    # Loss/latency on a physical link does not, by itself,
                    # identify which endpoint owns the defect.  Only a probe
                    # that explicitly reports the failing direction may set a
                    # fault endpoint. Otherwise retain the attachment hint
                    # as weak ordering support, not invented directionality.
                    preferred_device = str(evidence.metadata.get("fault_endpoint_device") or suspect_attachment or "")
                    preferred_interface = str(evidence.metadata.get("fault_endpoint_interface") or "")
                    for endpoint in (link.physical.endpoint_a, link.physical.endpoint_b):
                        if endpoint.device == preferred_device and (
                            not preferred_interface or endpoint.canonical_interface == preferred_interface
                        ):
                            key = (endpoint.device, endpoint.canonical_interface)
                            if evidence.metadata.get("fault_endpoint_device"):
                                causal_endpoint_support[key] = max(
                                    causal_endpoint_support[key],
                                    weight * 10.0,
                                )
                            else:
                                symptom_endpoint_support[key] = max(
                                    symptom_endpoint_support[key],
                                    weight * 2.0,
                                )

    endpoint_keys = set(causal_endpoint_support) | set(symptom_endpoint_support)
    endpoint_support = {
        key: max(causal_endpoint_support.get(key, 0.0), symptom_endpoint_support.get(key, 0.0)) for key in endpoint_keys
    }

    return PathEvidenceAnalysis(
        abnormal_support=_normalize(dict(abnormal)),
        healthy_support=_normalize(dict(healthy)),
        active_probe_support=_normalize(dict(active)),
        counter_support=_normalize(dict(counters)),
        configuration_support=_normalize(dict(differences)),
        evidence_ids=dict(ids),
        causal_endpoint_support=dict(causal_endpoint_support),
        symptom_endpoint_support=dict(symptom_endpoint_support),
        endpoint_support=dict(endpoint_support),
        unresolved_paths=unresolved,
    )


__all__ = [
    "PathEvidenceAnalysis",
    "analyze_path_evidence",
    "has_unresolved_ecmp_ambiguity",
    "scope_path_evidence",
]
