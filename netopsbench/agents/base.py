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
    ground_truth: Mapping[str, Any] | None = None
    tools: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = ["AgentVerdict", "VALID_AGENT_VERDICTS", "DiagnosticContext"]
