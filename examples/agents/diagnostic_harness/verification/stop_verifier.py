"""Final stop check combining diagnosability and schema validity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import DiagnosabilityDecision, ValidationReport
from .schema_validator import FinalResultValidator


@dataclass(frozen=True)
class StopVerification:
    can_submit: bool
    reason: str
    schema: ValidationReport


class StopVerifier:
    def __init__(self, validator: FinalResultValidator):
        self.validator = validator

    def verify(self, result: Any, *, decision: DiagnosabilityDecision, topology: Any) -> StopVerification:
        schema = self.validator.validate(result, topology=topology)
        if not decision.can_submit:
            return StopVerification(False, decision.reason, schema)
        if not schema.valid:
            return StopVerification(False, "Final result failed schema validation.", schema)
        return StopVerification(True, "Diagnosability and final schema checks passed.", schema)


__all__ = ["StopVerification", "StopVerifier"]
