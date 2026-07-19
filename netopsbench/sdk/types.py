"""Shared public NetOpsBench SDK types."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from netopsbench.agents.base import DiagnosisResult, DiagnosticContext
from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec


@dataclass(frozen=True)
class PlatformDefaults:
    """Default runtime settings for a NetOpsBench platform instance."""

    scale: str | None = None
    workers: int | None = None
    artifacts_dir: str | Path | None = None
    runtime_root_dir: str | Path | None = None
    keep_runtime: bool | None = None


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
    def evaluate(self, context: DiagnosticContext, result: DiagnosisResult) -> Mapping[str, Any]: ...


__all__ = [
    "ScenarioSpec",
    "EpisodeSpec",
    "PlatformDefaults",
    "DiagnosticContext",
    "DiagnosisResult",
    "FaultContext",
    "FaultExecutionResult",
    "ScenarioEvaluator",
]
