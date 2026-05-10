"""Pydantic output schema shared by all providers."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class DiagnosisLocation(BaseModel):
    device: Optional[str] = None
    interface: Optional[str] = None


class DiagnosisOutput(BaseModel):
    # The benchmark scorer requires an exact string match against one of these
    # three values.  Using Literal advertises the enum to the LLM through the
    # tool's JSON schema and lets pydantic reject free-form synonyms like
    # "fault", "fault_found", "fault_confirmed" that earlier runs produced.
    verdict: Literal["fault_detected", "network_healthy", "inconclusive"] = Field(
        description=(
            "Final diagnosis verdict. Must be exactly one of: "
            "'fault_detected' (a fault is present), "
            "'network_healthy' (no fault), "
            "'inconclusive' (insufficient evidence). "
            "Do NOT invent other values such as 'fault', 'fault_found', or 'fault_confirmed'."
        )
    )
    fault_type: Optional[str] = None
    location: DiagnosisLocation = Field(default_factory=DiagnosisLocation)
    evidence: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reasoning: str = ""
