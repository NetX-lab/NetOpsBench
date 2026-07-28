"""Public scenario-based interactive simulator API."""

from __future__ import annotations

import logging
import threading
import weakref
from pathlib import Path
from typing import Any

from netopsbench.models.profiles import ScaleRegistry
from netopsbench.models.scenario import ScenarioSpec
from netopsbench.platform.incident.context import (
    build_canonical_observation,
    build_public_case_id,
    build_public_symptoms,
)
from netopsbench.platform.incident.engine import (
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
from netopsbench.platform.scenario.parser import parse_scenario_file
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
        scale_registry: ScaleRegistry,
        runtime_manager: Any,
        fault_registry: Any = None,
    ):
        self.scale_registry = scale_registry
        self.runtime_manager = runtime_manager
        self.fault_registry = fault_registry
        self._lease_pool: RuntimeLeasePool | None = None
        self._pool_config: tuple[int, int] | None = None
        self._environments: weakref.WeakSet[DiagnosticEnvironment] = weakref.WeakSet()
        self._incidents: weakref.WeakSet[PreparedIncident] = weakref.WeakSet()
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
            on_close=self._discard_environment,
        )
        with self._lock:
            self._environments.add(environment)
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
        if incident.state is IncidentState.ACTIVE:
            with self._lock:
                self._incidents.add(incident)
        return incident

    def close(self) -> None:
        with self._lock:
            environments = list(self._environments)
            incidents = list(self._incidents)
            lease_pool = self._lease_pool
        failures: list[str] = []
        for environment in environments:
            try:
                environment.close()
            except Exception as exc:
                logger.warning("Failed to close simulator environment", exc_info=True)
                failures.append(f"environment: {type(exc).__name__}: {exc}")
        for incident in incidents:
            try:
                incident.close()
            except Exception as exc:
                logger.warning("Failed to close prepared incident", exc_info=True)
                failures.append(f"incident: {type(exc).__name__}: {exc}")
        if lease_pool is not None:
            try:
                lease_pool.drain()
            except Exception as exc:
                logger.warning("Failed to drain simulator runtime leases", exc_info=True)
                failures.append(f"runtime_pool: {type(exc).__name__}: {exc}")
        if failures:
            raise RuntimeError("Simulator manager close failed: " + "; ".join(failures))
        with self._lock:
            self._environments.clear()
            self._incidents.clear()
            if self._lease_pool is lease_pool:
                self._lease_pool = None
                self._pool_config = None

    def _engine(self, config: SimulatorConfig) -> IncidentEngine:
        physical_config = (config.max_active_runtimes, config.orphan_lease_ttl_seconds)
        with self._lock:
            if self._lease_pool is None:
                self._lease_pool = RuntimeLeasePool(
                    self.runtime_manager,
                    self.scale_registry,
                    config,
                    self.fault_registry,
                )
                self._pool_config = physical_config
            elif self._pool_config != physical_config:
                raise ValueError(
                    "Simulator runtime pool capacity is fixed after first use; "
                    f"expected max_active_runtimes/orphan_ttl={self._pool_config}, got {physical_config}"
                )
            leases = self._lease_pool
            assert leases is not None
        return IncidentEngine(
            lambda: RuntimeEpisodeBackend(leases, self.scale_registry),
            default_session_config=config,
            on_close=self._discard_incident,
        )

    def _discard_environment(self, environment: DiagnosticEnvironment) -> None:
        with self._lock:
            self._environments.discard(environment)

    def _discard_incident(self, incident: PreparedIncident) -> None:
        with self._lock:
            self._incidents.discard(incident)

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
    "IncidentEngine",
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
    "build_canonical_observation",
    "build_public_case_id",
    "build_public_symptoms",
]
