"""Public scenario-based interactive simulator API."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from netopsbench.models.profiles import ScaleRegistry
from netopsbench.models.scenario import ScenarioSpec
from netopsbench.platform.scenario.parser import parse_scenario_file
from netopsbench.platform.simulator.engine import (
    CleanupStatus,
    DiagnosticSession,
    ExecutionFailure,
    FailureDomain,
    IncidentEngine,
    IncidentState,
    PreparedIncident,
    SessionState,
    TerminationReason,
)
from netopsbench.platform.simulator.environment import (
    AgentUsage,
    DiagnosisLocation,
    DiagnosisSubmission,
    DiagnosticEnvironment,
    ResetResult,
    SimulatorConfig,
    StepResult,
    SubmitDiagnosisAction,
    ToolAction,
)
from netopsbench.platform.simulator.runtime import RuntimeEpisodeBackend, RuntimeLeasePool
from netopsbench.platform.toolkit.mcp.registry import tool_schemas, validate_tool_call

logger = logging.getLogger(__name__)


def simulator_tool_schemas() -> list[dict[str, Any]]:
    """Return the versioned model-facing simulator tool contract."""
    return tool_schemas()


def validate_tool_action(action: ToolAction) -> bool:
    """Validate an action against the same typed registry as FastMCP."""
    return validate_tool_call(action.name, action.arguments)


class SimulatorManager:
    """Create generic diagnostic incidents over warm NetOpsBench runtimes."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        scale_registry: ScaleRegistry,
        runtime_manager: Any,
        fault_registry: Any = None,
    ):
        self.workspace = Path(workspace)
        self.scale_registry = scale_registry
        self.runtime_manager = runtime_manager
        self.fault_registry = fault_registry
        self._lease_pools: dict[str, RuntimeLeasePool] = {}
        self._environments: list[DiagnosticEnvironment] = []
        self._incidents: list[PreparedIncident] = []
        self._lock = threading.RLock()

    def create(
        self,
        *,
        scenario: ScenarioSpec | str | Path,
        config: SimulatorConfig | None = None,
    ) -> DiagnosticEnvironment:
        resolved_scenario = self._scenario(scenario)
        resolved_config = config or SimulatorConfig()
        environment = DiagnosticEnvironment(
            self._engine(resolved_config),
            resolved_scenario,
            resolved_config,
        )
        with self._lock:
            self._environments.append(environment)
        return environment

    def prepare(
        self,
        *,
        scenario: ScenarioSpec | str | Path,
        config: SimulatorConfig | None = None,
    ) -> PreparedIncident:
        resolved_scenario = self._scenario(scenario)
        resolved_config = config or SimulatorConfig()
        incident = self._engine(resolved_config).prepare(resolved_scenario)
        with self._lock:
            self._incidents.append(incident)
        return incident

    def describe_tools(self) -> list[dict[str, Any]]:
        """Return the same model-facing schemas used by direct and MCP tools."""
        return tool_schemas()

    def close(self) -> None:
        with self._lock:
            environments = list(self._environments)
            incidents = list(self._incidents)
            lease_pools = list(self._lease_pools.values())
            self._environments.clear()
            self._incidents.clear()
            self._lease_pools.clear()
        for environment in environments:
            try:
                environment.close()
            except Exception:
                logger.warning("Failed to close simulator environment", exc_info=True)
        for incident in incidents:
            try:
                incident.close()
            except Exception:
                logger.warning("Failed to close prepared incident", exc_info=True)
        for leases in lease_pools:
            try:
                leases.drain()
            except Exception:
                logger.warning("Failed to drain simulator runtime leases", exc_info=True)

    def _engine(self, config: SimulatorConfig) -> IncidentEngine:
        key = config.model_dump_json()
        with self._lock:
            leases = self._lease_pools.get(key)
            if leases is None:
                leases = RuntimeLeasePool(
                    self.runtime_manager,
                    self.scale_registry,
                    config,
                    self.fault_registry,
                )
                self._lease_pools[key] = leases
        return IncidentEngine(lambda: RuntimeEpisodeBackend(leases, self.scale_registry))

    def _scenario(self, value: ScenarioSpec | str | Path) -> ScenarioSpec:
        scenario = value if isinstance(value, ScenarioSpec) else parse_scenario_file(value)
        self.scale_registry.get(scenario.scale)
        return scenario


__all__ = [
    "AgentUsage",
    "CleanupStatus",
    "DiagnosisLocation",
    "DiagnosisSubmission",
    "DiagnosticEnvironment",
    "DiagnosticSession",
    "ExecutionFailure",
    "FailureDomain",
    "IncidentState",
    "PreparedIncident",
    "ResetResult",
    "SessionState",
    "SimulatorConfig",
    "SimulatorManager",
    "StepResult",
    "SubmitDiagnosisAction",
    "TerminationReason",
    "ToolAction",
    "simulator_tool_schemas",
    "validate_tool_action",
]
