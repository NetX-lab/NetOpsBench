"""Slim shared diagnostic context contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

AgentVerdict = Literal["fault_detected", "network_healthy", "inconclusive"]
VALID_AGENT_VERDICTS: tuple[str, ...] = ("fault_detected", "network_healthy", "inconclusive")


@dataclass
class DiagnosticContext:
    """Shared machine-readable diagnostic state passed to public agents."""

    scenario_id: str
    topology: Mapping[str, Any]
    symptoms: Mapping[str, Any]
    tools: Any = None
    trace: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DiagnosisResult:
    """Result returned by a diagnostic agent."""

    agent_name: str
    verdict: str
    success: bool = True
    findings: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    reasoning: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = ["AgentVerdict", "VALID_AGENT_VERDICTS", "DiagnosticContext", "DiagnosisResult"]
