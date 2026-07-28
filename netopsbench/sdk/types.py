"""Shared public NetOpsBench SDK types."""

from dataclasses import dataclass, field
from typing import Any

from netopsbench.agents.base import DiagnosisResult, DiagnosticContext
from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec


@dataclass(frozen=True)
class FaultContext:
    """Shared fault execution context."""

    fault_type: str
    target_device: str
    target_interface: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    container_names: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FaultExecutionResult:
    """Result of a fault injection or recovery action."""

    fault_type: str
    success: bool
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


__all__ = [
    "ScenarioSpec",
    "EpisodeSpec",
    "DiagnosticContext",
    "DiagnosisResult",
    "FaultContext",
    "FaultExecutionResult",
]
