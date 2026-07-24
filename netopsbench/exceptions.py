"""Dependency-neutral public exception hierarchy.

Internal packages may import these types without depending on ``netopsbench.sdk``.
The SDK re-exports the same class objects for its stable public API.
"""

from __future__ import annotations

from typing import Any


class NetOpsBenchError(Exception):
    """Root of every NetOpsBench public exception."""


class ScenarioError(NetOpsBenchError):
    """Base class for scenario-related errors."""


class ScenarioValidationError(ScenarioError, ValueError):
    """A scenario file failed schema validation."""


class FaultRegistryError(NetOpsBenchError):
    """Base class for fault registry errors."""


class FaultNotFoundError(FaultRegistryError, KeyError):
    """Requested fault name is not registered."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return Exception.__str__(self)


class FaultValidationError(FaultRegistryError, ValueError):
    """Fault parameters failed validation."""


class AgentError(NetOpsBenchError):
    """Base class for agent-related errors."""


class AgentTimeoutError(AgentError, TimeoutError):
    """An agent diagnosis call exceeded the configured timeout."""


class AgentDiagnosisError(AgentError, RuntimeError):
    """An agent diagnosis call failed or returned an invalid result."""


class RuntimeProvisionError(NetOpsBenchError, RuntimeError):
    """A public runtime lifecycle operation failed."""


class RunFailedError(NetOpsBenchError):
    """A benchmark run completed with at least one failed scenario."""

    def __init__(self, message: str, *, report: Any | None = None) -> None:
        super().__init__(message)
        self.report = report


__all__ = [
    "AgentDiagnosisError",
    "AgentError",
    "AgentTimeoutError",
    "FaultNotFoundError",
    "FaultRegistryError",
    "FaultValidationError",
    "NetOpsBenchError",
    "RunFailedError",
    "RuntimeProvisionError",
    "ScenarioError",
    "ScenarioValidationError",
]
