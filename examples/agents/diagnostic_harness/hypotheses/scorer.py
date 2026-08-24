"""Configured support/contradiction scoring with simple softmax."""

from __future__ import annotations

import math
from collections.abc import Mapping

from ..config import HypothesisConfig
from ..evidence.validator import can_support_fault
from ..models import Evidence, Hypothesis, RankedInterfaceCandidate
from .impairment_matrix import evidence_signal
from .model import new_hypotheses


class HypothesisScorer:
    def __init__(self, config: HypothesisConfig | None = None):
        self.config = config or HypothesisConfig()

    def score(
        self,
        evidence_items: list[Evidence],
        *,
        interface_candidate: RankedInterfaceCandidate | None = None,
    ) -> dict[str, Hypothesis]:
        hypotheses = new_hypotheses(interface_candidate)
        observed_signals: set[str] = set()
        # Correlated flow rows from one telemetry source should not multiply
        # confidence merely because a topology has more clients.
        contributions: dict[tuple[str, str], tuple[float, Evidence]] = {}
        relations: dict[str, dict[str, list[str]]] = {
            fault_type: {"support": [], "contradict": []} for fault_type in hypotheses
        }
        for evidence in evidence_items:
            if not can_support_fault(evidence):
                continue
            signal = evidence_signal(evidence, self.config)
            if signal is None:
                continue
            observed_signals.add(signal)
            for fault_type, weight in self.config.matrix.get(signal, {}).items():
                if fault_type not in hypotheses or weight == 0:
                    continue
                hypothesis = hypotheses[fault_type]
                scope_weight = self._scope_weight(evidence, hypothesis, contradiction=weight < 0)
                weighted = evidence.reliability * float(weight) * scope_weight
                if weighted == 0:
                    continue
                key = (fault_type, self._independence_key(evidence))
                current = contributions.get(key)
                if current is None or abs(weighted) > abs(current[0]):
                    contributions[key] = (weighted, evidence)

        for (fault_type, _group), (weighted, evidence) in contributions.items():
            relation = "support" if weighted > 0 else "contradict"
            relations[fault_type][relation].append(evidence.evidence_id)

        for fault_type, hypothesis in hypotheses.items():
            hypothesis.score = self.config.prior + sum(
                weighted
                for (candidate_type, _group), (weighted, _evidence) in contributions.items()
                if candidate_type == fault_type
            )
            hypothesis.supporting_evidence = list(dict.fromkeys(relations[fault_type]["support"]))
            hypothesis.contradicting_evidence = list(dict.fromkeys(relations[fault_type]["contradict"]))
            hypothesis.missing_evidence = [
                requirement for requirement in hypothesis.required_evidence if requirement not in observed_signals
            ]

        # Receiving a payload with a verified application-level checksum
        # mismatch is a discriminator, not another weak symptom.  A topology
        # may expose the same missing packets through episode, summary and
        # hotspot views; those correlated rows must not make pure loss outrank
        # the causal integrity observation.
        integrity_failures = [
            item
            for item in evidence_items
            if can_support_fault(item) and item.category == "payload_integrity_failure" and bool(item.value)
        ]
        if integrity_failures:
            corruption = hypotheses["packet_corruption"]
            loss = hypotheses["packet_loss"]
            loss.score = min(
                loss.score,
                corruption.score - self.config.corruption_dominance_score_margin,
            )
            loss.contradicting_evidence = list(
                dict.fromkeys((*loss.contradicting_evidence, *(item.evidence_id for item in integrity_failures)))
            )

        # Random loss can be observed downstream of an MTU mismatch, but it
        # must not outrank the causal mechanism once two independent sources
        # agree: a stable DF size threshold and a peer MTU difference on the
        # ranked path.  This is intentionally a pairwise discriminator rather
        # than a global MTU weight increase.
        mtu_discriminators = self._mtu_discriminators(evidence_items, interface_candidate)
        if mtu_discriminators and not integrity_failures:
            mtu = hypotheses["mtu_mismatch"]
            for fault_type in ("packet_loss", "packet_corruption"):
                alternative = hypotheses[fault_type]
                alternative.score = min(
                    alternative.score,
                    mtu.score - self.config.mtu_dominance_score_margin,
                )
                alternative.contradicting_evidence = list(
                    dict.fromkeys(
                        (
                            *alternative.contradicting_evidence,
                            *(item.evidence_id for item in mtu_discriminators),
                        )
                    )
                )

        maximum = max((hypothesis.score for hypothesis in hypotheses.values()), default=0.0)
        exponentials = {key: math.exp(hypothesis.score - maximum) for key, hypothesis in hypotheses.items()}
        total = sum(exponentials.values()) or 1.0
        for key, hypothesis in hypotheses.items():
            hypothesis.probability = exponentials[key] / total
        return hypotheses

    @classmethod
    def _mtu_discriminators(
        cls,
        evidence_items: list[Evidence],
        candidate: RankedInterfaceCandidate | None,
    ) -> tuple[Evidence, ...]:
        if candidate is None:
            return ()
        thresholds = [
            item
            for item in evidence_items
            if can_support_fault(item)
            and item.category == "packet_size_threshold"
            and cls._stable_size_threshold(item)
            and cls._applies_to_candidate(item, candidate)
        ]
        differences = [
            item
            for item in evidence_items
            if can_support_fault(item)
            and item.category == "configuration_difference"
            and cls._concrete_peer_mtu_difference(item)
            and cls._applies_to_candidate(item, candidate)
        ]
        for threshold in thresholds:
            threshold_key = cls._independence_key(threshold)
            for difference in differences:
                if cls._independence_key(difference) != threshold_key:
                    return threshold, difference
        return ()

    @staticmethod
    def _stable_size_threshold(evidence: Evidence) -> bool:
        value = evidence.value if isinstance(evidence.value, Mapping) else {}
        try:
            largest_success = int(value["largest_successful_payload_size"])
            smallest_failure = int(value["smallest_failed_payload_size"])
        except (KeyError, TypeError, ValueError):
            return False
        return bool(value.get("size_dependent_failure")) and largest_success < smallest_failure

    @staticmethod
    def _concrete_peer_mtu_difference(evidence: Evidence) -> bool:
        value = evidence.value if isinstance(evidence.value, Mapping) else {}
        if value.get("field") not in {None, "mtu"} or not value.get("different"):
            return False
        try:
            return int(value["local_mtu"]) != int(value["peer_mtu"])
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _applies_to_candidate(evidence: Evidence, candidate: RankedInterfaceCandidate) -> bool:
        if evidence.evidence_id in candidate.evidence_ids:
            return True
        if evidence.metadata.get("link_id") == candidate.link_id:
            return True
        endpoints = {
            f"{candidate.primary_device}:{candidate.primary_interface}",
            f"{candidate.peer_device}:{candidate.peer_interface}",
        }
        if evidence.entity_type == "interface" and evidence.entity_id in endpoints:
            return True
        if candidate.link_id in evidence.covered_links:
            return True
        if evidence.observed_path is not None and candidate.link_id in evidence.observed_path:
            return True
        return any(candidate.link_id in path for path in evidence.possible_paths)

    @staticmethod
    def _independence_key(evidence: Evidence) -> str:
        """Conservatively group rows when the collector did not identify a batch.

        Collectors that can prove independence set ``independence_key``.  The
        fallback deliberately groups by source (and probe invocation), rather
        than by entity/timestamp: a large topology must not gain confidence
        merely because the same telemetry batch contains more rows.
        """
        if evidence.origin.name != "ACTIVE_PROBE" and "pingmesh" in evidence.source.lower():
            signal_group = "latency" if evidence.category in {"latency_median", "latency_p95"} else evidence.category
            window = evidence.metadata.get("observation_window_id") or evidence.metadata.get("window_id") or "episode"
            return f"passive:pingmesh:{window}:{signal_group}"
        if evidence.independence_key:
            return evidence.independence_key
        if evidence.probe_id:
            return f"probe:{evidence.source}:{evidence.probe_id}"
        return f"observation:{evidence.source}"

    @staticmethod
    def _scope_weight(evidence: Evidence, hypothesis: Hypothesis, *, contradiction: bool) -> float:
        """Bind both support and contradiction to the observed path scope.

        Exact-link observations apply only to that link.  An unknown ECMP path
        may support candidates inside its possible path set, but a healthy
        sample cannot refute any particular member because the actual member
        is unknown.  This applies equally to harness probes and structured
        observations recovered from a base-agent tool trace.
        """
        if evidence.entity_type != "path":
            return 1.0
        if hypothesis.link_id is None:
            # Before topology ranking, a path anomaly may classify the fault
            # family but cannot localize it. Conversely, one scoped healthy
            # path cannot globally refute the family.
            return 0.0 if contradiction else 1.0
        if evidence.covered_links:
            return 1.0 if hypothesis.link_id in evidence.covered_links else 0.0
        if evidence.observed_path is not None:
            return 1.0 if hypothesis.link_id in evidence.observed_path else 0.0
        if evidence.possible_paths:
            if contradiction:
                return 0.0
            possible_links = {link_id for path in evidence.possible_paths for link_id in path}
            return 1.0 if hypothesis.link_id in possible_links else 0.0
        return 1.0


__all__ = ["HypothesisScorer"]
