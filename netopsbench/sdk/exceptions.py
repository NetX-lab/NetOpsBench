"""Public NetOpsBench SDK exception hierarchy.

A small, stable set of exception types so that user code can catch specific
error classes instead of branching on generic ``RuntimeError`` / ``ValueError``
messages.

The hierarchy is intentionally shallow:

* :class:`NetOpsBenchError` — root for every public SDK error.
* :class:`ConfigurationError` — invalid configuration, missing env vars,
  unreadable workspace.
* :class:`ScenarioError` / :class:`ScenarioValidationError` — scenario YAML
  loading and validation failures.
* :class:`FaultRegistryError` / :class:`FaultNotFoundError` /
  :class:`FaultValidationError` — fault registry interactions.
* :class:`AgentError` / :class:`AgentTimeoutError` /
  :class:`AgentDiagnosisError` — agent lifecycle and diagnose() failures.
* :class:`RuntimeProvisionError` — runtime/topology provisioning failures.
* :class:`RunFailedError` — a benchmark ``RunHandle`` completed but at least
  one scenario failed; raised by ``RunHandle.wait(raise_on_failure=True)``.
"""

from __future__ import annotations

from typing import Any


class NetOpsBenchError(Exception):
    """Root of every NetOpsBench public SDK exception."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ConfigurationError(NetOpsBenchError):
    """Invalid or missing configuration (env var, workspace path, etc.)."""


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------


class ScenarioError(NetOpsBenchError):
    """Base class for scenario-related errors."""


class ScenarioValidationError(ScenarioError):
    """A scenario file failed schema validation."""


# ---------------------------------------------------------------------------
# Faults
# ---------------------------------------------------------------------------


class FaultRegistryError(NetOpsBenchError):
    """Base class for fault registry errors."""


class FaultNotFoundError(FaultRegistryError, KeyError):
    """Requested fault name is not registered.

    Inherits :class:`KeyError` so existing ``except KeyError`` blocks continue
    to work for backward compatibility.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return Exception.__str__(self)


class FaultValidationError(FaultRegistryError, ValueError):
    """Fault parameters failed validation.

    Inherits :class:`ValueError` for backward compatibility with callers that
    expected the previous generic exception type.
    """


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class AgentError(NetOpsBenchError):
    """Base class for agent-related errors."""


class AgentTimeoutError(AgentError, TimeoutError):
    """An agent's diagnose() call exceeded the configured timeout."""


class AgentDiagnosisError(AgentError):
    """Agent's diagnose() raised or returned an invalid result."""


# ---------------------------------------------------------------------------
# Runtime / sessions
# ---------------------------------------------------------------------------


class RuntimeProvisionError(NetOpsBenchError):
    """Failed to provision or tear down a runtime topology."""


class RunFailedError(NetOpsBenchError):
    """A benchmark run completed but at least one scenario failed.

    Raised by :meth:`netopsbench.sdk.reports.RunHandle.wait` when called with
    ``raise_on_failure=True`` and the resulting report indicates failure.

    Attributes:
        report: The :class:`BenchmarkReport` produced by the failed run; users
            can still inspect ``report.summary`` / ``report.scenario_summaries``
            after catching this error.
    """

    def __init__(self, message: str, *, report: Any | None = None) -> None:
        super().__init__(message)
        self.report = report


__all__ = [
    "NetOpsBenchError",
    "ConfigurationError",
    "ScenarioError",
    "ScenarioValidationError",
    "FaultRegistryError",
    "FaultNotFoundError",
    "FaultValidationError",
    "AgentError",
    "AgentTimeoutError",
    "AgentDiagnosisError",
    "RuntimeProvisionError",
    "RunFailedError",
]
