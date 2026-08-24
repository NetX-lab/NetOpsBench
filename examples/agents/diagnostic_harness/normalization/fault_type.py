"""Taxonomy-constrained fault-type normalization."""

from __future__ import annotations

from collections.abc import Mapping

from netopsbench.evaluator.fault_type_judge import canonicalize_fault_type, supported_fault_types

from ..config import default_fault_type_aliases
from ..models import FaultTypeNormalization


def _normalization_key(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


class FaultTypeNormalizer:
    """Normalize only labels backed by the live benchmark taxonomy."""

    def __init__(self, aliases: Mapping[str, str] | None = None):
        configured = default_fault_type_aliases()
        if aliases is not None:
            configured.update({_normalization_key(key): value for key, value in aliases.items()})
        self.aliases = configured
        self.canonical_labels = frozenset(supported_fault_types())

    def normalize(self, fault_type: str | None) -> FaultTypeNormalization:
        if fault_type is None or not str(fault_type).strip():
            return FaultTypeNormalization(original=fault_type, value=None, is_canonical=True)

        original = str(fault_type).strip()
        key = _normalization_key(original)
        aliased = self.aliases.get(key, key)
        canonical = canonicalize_fault_type(aliased)
        if canonical in self.canonical_labels:
            changed = canonical != original
            return FaultTypeNormalization(
                original=original,
                value=canonical,
                is_canonical=True,
                normalized_from=original if changed else None,
            )

        return FaultTypeNormalization(
            original=original,
            value=original,
            is_canonical=False,
            validation_error=f"unknown fault type: {original}",
        )


__all__ = ["FaultTypeNormalizer"]
