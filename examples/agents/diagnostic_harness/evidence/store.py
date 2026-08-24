"""Bounded structured evidence storage for one diagnosis."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from ..models import Evidence


def _timestamp_key(evidence: Evidence) -> datetime:
    return evidence.timestamp or datetime.min.replace(tzinfo=UTC)


def _compact_value(value: Any, limit: int = 240) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        text = str(value) if isinstance(value, str) else value
        if isinstance(text, str) and len(text) > limit:
            return text[: limit - 3].rstrip() + "..."
        return text
    if isinstance(value, dict):
        return {str(key): _compact_value(item, limit=80) for key, item in list(value.items())[:12]}
    if isinstance(value, (list, tuple)):
        return [_compact_value(item, limit=80) for item in list(value)[:12]]
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


class EvidenceStore:
    def __init__(self, evidence: Iterable[Evidence] | None = None):
        self._items: dict[str, Evidence] = {}
        self._conflicts: set[frozenset[str]] = set()
        for item in evidence or ():
            self.add(item)

    def add(self, evidence: Evidence) -> None:
        existing = self._items.get(evidence.evidence_id)
        if existing is not None and existing != evidence:
            raise ValueError(f"evidence_id already exists with different content: {evidence.evidence_id}")
        self._items[evidence.evidence_id] = evidence

    def add_all(self, evidence: Iterable[Evidence]) -> None:
        for item in evidence:
            self.add(item)

    def replace(self, evidence: Evidence) -> None:
        """Replace one item after deterministic enrichment, preserving its ID."""
        if evidence.evidence_id not in self._items:
            raise KeyError(f"evidence_id does not exist: {evidence.evidence_id}")
        self._items[evidence.evidence_id] = evidence

    def replace_all(self, evidence: Iterable[Evidence]) -> None:
        for item in evidence:
            self.replace(item)

    def apply_reconciliation(self, evidence: Iterable[Evidence]) -> None:
        """Apply one full-ledger reconciliation without silently dropping data.

        Reconciliation may deterministically revise existing records and add
        derived certificates.  It may not remove an observation from the audit
        ledger.  Keeping that contract here prevents callers from confusing a
        derived Evidence item with an in-place replacement.
        """
        reconciled: dict[str, Evidence] = {}
        for item in evidence:
            existing = reconciled.get(item.evidence_id)
            if existing is not None and existing != item:
                raise ValueError(f"duplicate reconciled evidence_id: {item.evidence_id}")
            reconciled[item.evidence_id] = item
        missing = set(self._items).difference(reconciled)
        if missing:
            raise ValueError(f"reconciliation omitted existing evidence IDs: {sorted(missing)}")
        for item in reconciled.values():
            if item.evidence_id in self._items:
                self.replace(item)
            else:
                self.add(item)

    def get(self, evidence_id: str) -> Evidence | None:
        return self._items.get(evidence_id)

    def get_by_entity(self, entity: str, *, entity_type: str | None = None) -> list[Evidence]:
        return [
            item
            for item in self._items.values()
            if item.entity_id == entity and (entity_type is None or item.entity_type == entity_type)
        ]

    def get_by_category(self, category: str) -> list[Evidence]:
        return [item for item in self._items.values() if item.category == category]

    def get_recent(self, entity: str, category: str) -> Evidence | None:
        matches = [item for item in self.get_by_entity(entity) if item.category == category]
        return max(matches, key=_timestamp_key, default=None)

    def has_fresh(
        self,
        entity: str,
        category: str,
        *,
        max_age_seconds: float,
        now: datetime | None = None,
    ) -> bool:
        recent = self.get_recent(entity, category)
        if recent is None or recent.timestamp is None or recent.freshness <= 0:
            return False
        current = now or datetime.now(UTC)
        observed = recent.timestamp
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        return max(0.0, (current - observed).total_seconds()) <= max_age_seconds

    def mark_conflict(self, evidence_a: str | Evidence, evidence_b: str | Evidence) -> None:
        left = evidence_a.evidence_id if isinstance(evidence_a, Evidence) else evidence_a
        right = evidence_b.evidence_id if isinstance(evidence_b, Evidence) else evidence_b
        if left not in self._items or right not in self._items:
            raise KeyError("both conflicting evidence IDs must exist")
        if left == right:
            raise ValueError("evidence cannot conflict with itself")
        self._conflicts.add(frozenset((left, right)))

    @property
    def conflicts(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(tuple(sorted(pair)) for pair in self._conflicts))

    def summarize_for_llm(self, *, max_items: int = 20) -> list[dict[str, Any]]:
        ordered = sorted(self._items.values(), key=lambda item: (_timestamp_key(item), item.evidence_id), reverse=True)
        return [
            {
                "evidence_id": item.evidence_id,
                "entity": {"type": item.entity_type, "id": item.entity_id},
                "category": item.category,
                "value": _compact_value(item.value),
                "source": item.source,
                "reliability": item.reliability,
                "freshness": item.freshness,
                "probe_id": item.probe_id,
                "observed_path": item.observed_path,
                "possible_paths": item.possible_paths,
                "covered_links": item.covered_links,
                "path_observation_confidence": item.path_observation_confidence,
                "origin": item.origin.value,
                "independence_key": item.independence_key,
                "direction": item.direction.value,
                "supports_submission": item.supports_submission,
            }
            for item in ordered[:max_items]
        ]

    def all(self) -> list[Evidence]:
        return list(self._items.values())

    def __len__(self) -> int:
        return len(self._items)


__all__ = ["EvidenceStore"]
