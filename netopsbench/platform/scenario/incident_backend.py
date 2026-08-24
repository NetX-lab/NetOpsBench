"""ScenarioExecutor adapter for the shared incident engine."""

from __future__ import annotations

from typing import Any

from netopsbench.models.scenario import ScenarioSpec
from netopsbench.platform.incident.context import (
    build_canonical_observation,
    build_public_symptoms,
    build_topology_snapshot,
    extract_episode_pingmesh_query_window,
)
from netopsbench.platform.incident.contracts import ToolAction
from netopsbench.platform.scenario.episode_runner import observe_episode
from netopsbench.platform.scenario.observation import (
    baseline_gate_errors,
    observation_integrity_errors,
)
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
                reference = self.executor._capture_baseline_window()
                validation = self.executor._wait_and_observe(
                    int(reference["duration_seconds"]),
                    baseline_window=reference,
                )
                gate_errors = baseline_gate_errors(validation)
                if gate_errors:
                    raise RuntimeError("Healthy baseline is unavailable: " + "; ".join(gate_errors))
                self.baseline_window = {
                    "name": "baseline",
                    "start_time": validation["start_time"],
                    "end_time": validation["end_time"],
                    "duration_seconds": validation["duration_seconds"],
                }
        if self.baseline_window is None:
            raise RuntimeError("Incident preparation requires an explicit Pingmesh baseline window")
        self.episode_result = observe_episode(
            self.executor,
            scenario.episode,
            baseline_window=self.baseline_window,
        )
        integrity_errors = observation_integrity_errors(
            self.episode_result.get("observations") or {},
            # Positive fault episodes may legitimately contain local probe
            # errors/DF drops; preserve those as observations instead of
            # aborting the episode. Healthy baselines still require zero.
            allow_fault_local_errors=self.scenario.episode.fault_type != "none",
        )
        if integrity_errors:
            raise RuntimeError("Incident observation is incomplete: " + "; ".join(integrity_errors))
        self.toolkit = AgentToolkit(
            topology_dir=self.topology_dir,
            topology_metadata=self.executor.topology_metadata,
        )
        self.toolkit.influxdb_bucket = self.influxdb_bucket or self.executor.influxdb_bucket
        self.toolkit.topology_id = self.topology_id or self.executor.topology_id
        self.pingmesh_query_window = extract_episode_pingmesh_query_window(self.episode_result)
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
            case_id=None,
            topology=self.topology,
            symptoms=self.symptoms,
            include_case_id=False,
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
        if self.scenario is not None:
            self.cleanup_result = self.executor._cleanup_after_scenario(
                self.scenario,
                self.episode_result,
                baseline_window=self.baseline_window,
            )
            if self.episode_result is not None and "recovery" in self.cleanup_result:
                self.episode_result["recovery"] = self.cleanup_result["recovery"]
            if "validated_baseline" in self.cleanup_result:
                self.baseline_window = dict(self.cleanup_result["validated_baseline"])
            if not self.cleanup_result.get("success", False):
                raise RuntimeError(f"Scenario cleanup failed: {self.cleanup_result}")


__all__ = ["ExecutorIncidentBackend"]
