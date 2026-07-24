"""ScenarioExecutor adapter for the shared incident engine."""

from __future__ import annotations

from typing import Any

from netopsbench.models.scenario import ScenarioSpec
from netopsbench.platform.incident.context import (
    _extract_episode_pingmesh_query_window,
    build_canonical_observation,
    build_public_case_id,
    build_public_symptoms,
    build_topology_snapshot,
)
from netopsbench.platform.incident.contracts import ToolAction
from netopsbench.platform.scenario.episode_runner import observe_episode
from netopsbench.platform.toolkit.toolkit import AgentToolkit


class ExecutorIncidentBackend:
    """Low-level benchmark backend with no lifecycle state of its own."""

    def __init__(
        self,
        executor: Any,
        *,
        setup_traffic: bool = True,
        baseline_window: dict[str, Any] | None = None,
        influxdb_bucket: str | None = None,
        topology_id: str | None = None,
    ):
        self.executor = executor
        self.setup_traffic = setup_traffic
        self.baseline_window = baseline_window
        self.influxdb_bucket = influxdb_bucket
        self.topology_id = topology_id
        self.topology_dir = str(executor.topology_dir)
        self.scenario: ScenarioSpec | None = None
        self.episode_result: dict[str, Any] | None = None
        self.traffic_config: dict[str, Any] | None = None
        self.toolkit: Any = None
        self.topology: dict[str, Any] = {}
        self.symptoms: dict[str, Any] = {}
        self.pingmesh_query_window: dict[str, Any] = {}
        self.cleanup_result: dict[str, Any] | None = None
        self._finished = False

    def prepare(self, scenario: ScenarioSpec) -> dict[str, Any]:
        self.scenario = scenario
        if self.setup_traffic:
            controller = self.executor.traffic_controller
            traffic_ready = (
                controller is not None and bool(controller.active_flows) and controller.verify_active_flows()
            )
            if not traffic_ready:
                if controller is not None:
                    self.executor._stop_traffic()
                self.traffic_config = self.executor._setup_traffic(
                    scenario.scale,
                    scenario.traffic_profile,
                )
            if self.baseline_window is None:
                self.baseline_window = self.executor._capture_baseline_window()
        if self.baseline_window is None:
            raise RuntimeError("Incident preparation requires an explicit Pingmesh baseline window")
        self.episode_result = observe_episode(
            self.executor,
            scenario.episode,
            baseline_window=self.baseline_window,
        )
        self.toolkit = AgentToolkit(
            topology_dir=self.topology_dir,
            topology_metadata=self.executor.topology_metadata,
        )
        self.toolkit.influxdb_bucket = self.influxdb_bucket or self.executor.influxdb_bucket
        self.toolkit.topology_id = self.topology_id or self.executor.topology_id
        self.pingmesh_query_window = _extract_episode_pingmesh_query_window(self.episode_result)
        self.toolkit.set_pingmesh_time_window(
            self.pingmesh_query_window.get("start_time"),
            self.pingmesh_query_window.get("end_time"),
        )
        self.topology = build_topology_snapshot(self.toolkit)
        self.symptoms = build_public_symptoms(
            episode_result=self.episode_result,
            pingmesh_query_window=self.pingmesh_query_window,
        )
        return build_canonical_observation(
            case_id=build_public_case_id(
                scenario_id=scenario.id,
                episode_result=self.episode_result,
            ),
            topology=self.topology,
            symptoms=self.symptoms,
        )

    def call_tool(self, action: ToolAction) -> dict[str, Any]:
        if self.toolkit is None:
            raise RuntimeError("Incident backend is not prepared")
        method = getattr(self.toolkit, action.name, None)
        if not callable(method):
            raise ValueError(f"Toolkit action is not implemented: {action.name}")
        result = method(**action.arguments)
        return result.to_dict() if hasattr(result, "to_dict") else {"success": True, "data": result}

    def refresh(self, *, min_seconds: float = 0.0) -> None:
        del min_seconds

    def finish(self, *, broken: bool = False) -> None:
        if self._finished:
            return
        self._finished = True
        if self.setup_traffic and self.scenario is not None:
            self.cleanup_result = self.executor._cleanup_after_scenario(self.scenario, self.episode_result)
            if self.episode_result is not None and "recovery" in self.cleanup_result:
                self.episode_result["recovery"] = self.cleanup_result["recovery"]
            if not self.cleanup_result.get("success", False):
                raise RuntimeError(f"Scenario cleanup failed: {self.cleanup_result}")
            return
        if self.scenario is not None and not self.scenario.episode.is_healthy:
            recovery = self.executor._recover_fault()
            if self.episode_result is not None:
                self.episode_result["recovery"] = recovery
            if any(not item.get("recovered", False) for item in recovery):
                raise RuntimeError(f"Fault recovery failed: {recovery}")
            if recovery:
                self.executor.sleep(self.executor.post_recovery_wait_seconds)


__all__ = ["ExecutorIncidentBackend"]
