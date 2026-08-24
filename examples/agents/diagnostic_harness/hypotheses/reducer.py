"""One deterministic final reduction from Evidence to a gate decision."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..models import Evidence, Hypothesis, RankedInterfaceCandidate
from .diagnosability import DiagnosabilityGate
from .multi_fault import IndependentFault, MultiFaultAnalyzer
from .scorer import HypothesisScorer


@dataclass(frozen=True)
class DiagnosisReduction:
    hypotheses: dict[str, Hypothesis]
    gate_decision: Any
    secondary_faults: tuple[IndependentFault, ...]
    explained_secondary_evidence: frozenset[str]


class DiagnosisReducer:
    """Keep scoring, independent-domain extraction, and gating consistent."""

    def __init__(
        self,
        scorer: HypothesisScorer,
        gate: DiagnosabilityGate,
        multi_fault: MultiFaultAnalyzer,
    ) -> None:
        self._scorer = scorer
        self._gate = gate
        self._multi_fault = multi_fault

    def reduce(
        self,
        evidence: Sequence[Evidence],
        candidates: Sequence[RankedInterfaceCandidate],
        *,
        conflicts: Sequence[Any] = (),
        initial_device: str | None = None,
        initial_interface: str | None = None,
    ) -> DiagnosisReduction:
        interface_candidate = candidates[0] if candidates else None
        hypotheses = self._scorer.score(list(evidence), interface_candidate=interface_candidate)
        top = max(hypotheses.values(), key=lambda item: item.probability)
        secondary = self._multi_fault.detect(
            evidence,
            candidates,
            primary_fault_type=top.fault_type,
            primary_device=interface_candidate.primary_device if interface_candidate else None,
            primary_interface=interface_candidate.primary_interface if interface_candidate else None,
            primary_link_id=interface_candidate.link_id if interface_candidate else None,
        )
        explained = self._multi_fault.explained_evidence_ids(secondary)
        if explained:
            hypotheses = self._scorer.score(
                [item for item in evidence if item.evidence_id not in explained],
                interface_candidate=interface_candidate,
            )
            rescored_top = max(hypotheses.values(), key=lambda item: item.probability)
            if rescored_top.fault_type != top.fault_type:
                secondary = self._multi_fault.detect(
                    evidence,
                    candidates,
                    primary_fault_type=rescored_top.fault_type,
                    primary_device=interface_candidate.primary_device if interface_candidate else None,
                    primary_interface=interface_candidate.primary_interface if interface_candidate else None,
                    primary_link_id=interface_candidate.link_id if interface_candidate else None,
                )
                explained = self._multi_fault.explained_evidence_ids(secondary)
                hypotheses = self._scorer.score(
                    [item for item in evidence if item.evidence_id not in explained],
                    interface_candidate=interface_candidate,
                )
        gate = self._gate.analyze(
            hypotheses,
            list(evidence),
            list(candidates),
            conflicts=conflicts,
            initial_device=initial_device,
            initial_interface=initial_interface,
            explained_secondary_evidence=explained,
        )
        return DiagnosisReduction(hypotheses, gate, secondary, explained)


__all__ = ["DiagnosisReducer", "DiagnosisReduction"]
