"""One bounded observation-driven family transition."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import RouterConfig
from ..evidence.store import EvidenceStore
from ..evidence.validator import can_support_fault
from ..models import Evidence
from .symptom_profile import build_symptom_profile


@dataclass(frozen=True)
class FamilyReplan:
    family: str
    reason: str
    evidence_ids: tuple[str, ...]
    score: float


class BoundedFamilyReplanner:
    """Select at most one new family from structured network observations."""

    def __init__(self, config: RouterConfig | None = None):
        self.config = config or RouterConfig()

    def select(
        self,
        store: EvidenceStore,
        *,
        current_family: str | None,
        attempted_families: set[str],
        transitions: int,
        allowed_families: frozenset[str],
    ) -> FamilyReplan | None:
        if transitions >= self.config.max_family_replans:
            return None
        profile = build_symptom_profile(store, self.config)
        ranked = list(profile.ranked_families)
        discriminator = self._specific_discriminator_family(store)
        if discriminator in ranked:
            ranked.remove(discriminator)
            ranked.insert(0, discriminator)
        for observed_family in ranked:
            family = _verification_family(observed_family)
            if (
                family == current_family
                or family in attempted_families
                or family not in allowed_families
                or profile.family_scores[observed_family] < self.config.replan_min_family_score
            ):
                continue
            evidence_ids = profile.family_evidence_ids.get(observed_family, ())
            if not evidence_ids:
                continue
            return FamilyReplan(
                family=family,
                reason=f"observation_replan:{current_family or 'none'}->{family}",
                evidence_ids=evidence_ids,
                score=profile.family_scores[observed_family],
            )
        return None

    @staticmethod
    def _specific_discriminator_family(store: EvidenceStore) -> str | None:
        """Prefer causal signatures over their downstream loss symptom."""
        items = store.all()
        if any(
            item.category == "payload_integrity_failure" and can_support_fault(item) and bool(item.value)
            for item in items
        ):
            return "packet_corruption"
        if any(BoundedFamilyReplanner._is_size_dependent(item) for item in items):
            return "mtu"
        return None

    @staticmethod
    def _is_size_dependent(item: Evidence) -> bool:
        value = item.value if isinstance(item.value, dict) else {}
        return (
            item.category == "packet_size_threshold"
            and can_support_fault(item)
            and bool(value.get("size_dependent_failure"))
        )


def _verification_family(family: str) -> str:
    return "temporal_verification" if family == "link_flapping" else family


__all__ = ["BoundedFamilyReplanner", "FamilyReplan"]
