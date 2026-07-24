"""Dependency-neutral diagnostic incident configuration and action models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SimulatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_active_runtimes: int = Field(default=1, ge=1)
    max_tool_calls: int = Field(default=20, ge=1)
    max_agent_seconds: int = Field(default=300, ge=1)
    max_tool_result_bytes: int = Field(default=8_192, ge=1_024)
    orphan_lease_ttl_seconds: int = Field(default=3_600, ge=1)

    @model_validator(mode="after")
    def validate_lease_deadline(self) -> SimulatorConfig:
        minimum = self.max_agent_seconds + 300
        if self.orphan_lease_ttl_seconds < minimum:
            raise ValueError("orphan_lease_ttl_seconds must cover max_agent_seconds plus 300 seconds of cleanup grace")
        return self


class ToolAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["tool"] = "tool"
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class DiagnosisLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    device: str | None = None
    interface: str | None = None


class DiagnosisSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    verdict: Literal["fault_detected", "network_healthy", "inconclusive"]
    fault_type: str | None = None
    location: DiagnosisLocation = Field(default_factory=DiagnosisLocation)
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)
    reasoning: str = ""


class AgentUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class SubmitDiagnosisAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["submit_diagnosis"] = "submit_diagnosis"
    diagnosis: DiagnosisSubmission
    usage: AgentUsage = Field(default_factory=AgentUsage)


__all__ = [
    "AgentUsage",
    "DiagnosisLocation",
    "DiagnosisSubmission",
    "SimulatorConfig",
    "SubmitDiagnosisAction",
    "ToolAction",
]
