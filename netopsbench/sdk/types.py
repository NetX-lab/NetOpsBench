"""Shared public NetOpsBench SDK types."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from netopsbench.agents.base import DiagnosticContext


@dataclass(frozen=True)
class PlatformDefaults:
    """Default runtime settings for a NetOpsBench platform instance."""

    scale: str | None = None
    workers: int | None = None
    artifacts_dir: str | Path | None = None
    runtime_root_dir: str | Path | None = None
    keep_runtime: bool | None = None


@dataclass(frozen=True)
class EpisodeSpec:
    """Public scenario episode specification."""

    episode_id: str
    description: str = ""
    fault_type: str = "none"
    target_device: str | None = None
    target_interface: str | None = None
    target_prefix: str | None = None
    duration_seconds: int = 30
    stabilization_time: int = 10
    metadata: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScenarioSpec:
    """Public scenario specification."""

    scenario_id: str
    name: str
    description: str = ""
    topology_scale: str = "xs"
    traffic_profile: str = "standard"
    episodes: list[EpisodeSpec] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)


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


class ScenarioEvaluator(Protocol):
    """Protocol for evaluating a scenario or diagnosis result."""

    def evaluate(self, context: DiagnosticContext, result: DiagnosisResult) -> Mapping[str, Any]:
        """Evaluate a diagnosis against a diagnostic context."""


class SyncDiagnosticAgent(Protocol):
    """Protocol for synchronous diagnostic agents."""

    def diagnose(self, context: DiagnosticContext) -> DiagnosisResult:
        """Run a synchronous diagnosis."""


__all__ = [
    "PlatformDefaults",
    "ScenarioSpec",
    "EpisodeSpec",
    "DiagnosticContext",
    "DiagnosisResult",
    "FaultContext",
    "FaultExecutionResult",
    "ScenarioEvaluator",
    "SyncDiagnosticAgent",
]
