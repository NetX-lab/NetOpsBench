"""Safety rules for interpreting structured observations."""

from __future__ import annotations

from ..models import Evidence

NON_FAULT_CATEGORIES = frozenset({"tool_error", "missing_observation"})


def can_support_fault(evidence: Evidence) -> bool:
    """Tool failures and missing data can never support a fault hypothesis."""
    return evidence.supports_submission and evidence.category not in NON_FAULT_CATEGORIES and evidence.reliability > 0


def can_plan_from(evidence: Evidence) -> bool:
    """Return whether an observation may guide a bounded verification step.

    Planning trust is intentionally weaker than submission trust.  Tool
    errors and missing data remain unusable for either purpose.
    """
    return evidence.usable_for_planning and evidence.category not in NON_FAULT_CATEGORIES and evidence.reliability > 0


__all__ = ["NON_FAULT_CATEGORIES", "can_plan_from", "can_support_fault"]
