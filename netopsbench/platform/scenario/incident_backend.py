"""ScenarioExecutor adapter for the shared incident engine."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from netopsbench.models.scenario import ScenarioSpec
from netopsbench.platform.incident.context import (
    build_canonical_observation,
    build_public_case_id,
    build_public_symptoms,
    build_topology_snapshot,
    extract_episode_pingmesh_query_window,
)
from netopsbench.platform.incident.contracts import ToolAction
from netopsbench.platform.scenario.episode_runner import observe_episode
from netopsbench.platform.scenario.observation import (
    baseline_window_seconds,
    observation_integrity_errors,
)
from netopsbench.platform.toolkit.public import public_tool_result
from netopsbench.platform.toolkit.toolkit import AgentToolkit
from netopsbench.platform.utils.control import current_operation_control


class ExecutorIncidentBackend:
    """Low-level benchmark backend with no lifecycle state of its own."""

    def __init__(
        self,
        executor: Any,
        *,
        influxdb_bucket: str | None = None,
        topology_id: str | None = None,
    ):
        self.executor = executor
        self.baseline_window: dict[str, Any] | None = None
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
        self.baseline_preparation: dict[str, Any] = {"mode": "explicit"}
        self._scope = None

    def prepare(self, scenario: ScenarioSpec) -> dict[str, Any]:
        self.scenario = scenario
        from netopsbench.models.recovery import RecoveryPolicy

        policy = RecoveryPolicy.from_metadata(scenario.metadata)
        handoff = self.executor._baseline_handoff
        if self.executor.recovery_policy != policy:
            handoff.clear("recovery_policy_changed")
        self.executor.recovery_policy = policy
        handoff.max_cases = policy.baseline_max_cases
        if self.executor.injector.active_faults:
            raise RuntimeError("Runtime contains unresolved faults; recovery is required before preparation")
        if scenario.metadata.get("experiment") is not None:
            experiment_policy = self.executor.experiment
            window = experiment_policy.window_seconds(
                self.executor.current_manifest().require_pingmesh_plan().settings.complete_window_seconds
            )
            if (
                scenario.metadata["experiment"] != experiment_policy.model_dump(mode="json")
                or scenario.episode.duration_seconds != window
                or scenario.episode.stabilization_time
            ):
                raise ValueError("Campaign experiment configuration changed; regenerate scenarios in a new campaign")
            if scenario.episode.fault_type in {"link_flapping", "route_flapping"} and (
                scenario.episode.parameters.get("down_time") != experiment_policy.flapping_down_seconds
                or scenario.episode.parameters.get("up_time") != experiment_policy.flapping_up_seconds
            ):
                raise ValueError("Per-case periodic timing override is not allowed in a frozen campaign")
        with self.executor.timings.measure("traffic_readiness"):
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
            if traffic_ready:
                self.traffic_config = self.executor._traffic_config
        handoff = self.executor._baseline_handoff
        if self.baseline_window is None:
            self._scope = self.executor._baseline_scope(scenario)
            self.baseline_window, reason = handoff.take(
                self._scope, baseline_window_seconds(self.executor, self.executor.minimum_baseline_seconds)
            )
            self.baseline_preparation = {
                "mode": "reused" if self.baseline_window else "fresh",
                "reason": reason,
                "source_scenario_id": handoff.source if self.baseline_window else None,
                "scope_changes": handoff.last_changes,
            }
        if self.baseline_window is None:
            self.executor.calibrate(scenario, self.baseline_preparation.get("reason", "initial"))
            self._scope = self.executor._baseline_scope(scenario)
            self.baseline_window, reason = handoff.take(
                self._scope, baseline_window_seconds(self.executor, self.executor.minimum_baseline_seconds)
            )
            if self.baseline_window is None:
                raise RuntimeError("Calibrated baseline unavailable: " + reason)
        self.executor.timings.skip("baseline", "verified healthy window handoff")
        self.executor._case_initial_scope = self._scope
        self.baseline_preparation.update(
            uses=handoff.uses,
            recovery_policy=policy.model_dump(mode="json"),
            window=dict(self.baseline_window),
            age_seconds=(
                datetime.now(UTC) - datetime.fromisoformat(self.baseline_window["end_time"].replace("Z", "+00:00"))
            ).total_seconds(),
        )
        catalog_v2 = scenario.metadata.get("fault_catalog_version") == 2
        self.executor._case_affected_pairs = []
        self.executor._case_probe_pairs = []
        if catalog_v2 and not scenario.episode.is_healthy and scenario.episode.parameters.get("affected_candidates"):
            from netopsbench.platform.utils.interface_names import to_sonic_interface

            from .candidates import CandidatePaths

            episode = scenario.episode
            manifest = self.executor.current_manifest()
            scope = (id(manifest), manifest.topology_id, manifest.require_pingmesh_plan().settings.plan_id)
            if self.executor._candidate_scope != scope:
                self.executor._candidate_paths = CandidatePaths(manifest)
                self.executor._candidate_scope = scope
            candidates = self.executor._candidate_paths
            peer = next(
                (
                    peer
                    for (root, peer), interface in candidates.ports.items()
                    if root == episode.target_device and interface == to_sonic_interface(episode.target_interface)
                ),
                None,
            )
            self.executor._case_affected_pairs = candidates.affected_pairs(
                episode.fault_type,
                episode.target_device,
                peer,
                episode.parameters["affected_candidates"][0],
                reverse=(
                    episode.fault_type == "route_flapping" or episode.parameters.get("mechanism") == "export_filter"
                ),
            )
            self.executor._case_probe_pairs = self.executor._case_affected_pairs + episode.parameters.get(
                "control_candidates", []
            )
        from netopsbench.platform.observability.routes import collect_routes

        with self.executor.timings.measure(
            "baseline_routes",
            timeout_seconds=self.executor.scale_registry.get(scenario.scale).health_timeout_seconds,
        ):
            self.executor._case_routes = collect_routes(
                self.executor.current_manifest(),
                cancel_event=self.executor._cancel_event,
                deadline=time.monotonic() + self.executor.scale_registry.get(scenario.scale).health_timeout_seconds,
            )
        if any(
            row.get("error") or row.get("bgp_collection_ok") is False
            for row in self.executor._case_routes["nodes"].values()
        ):
            raise RuntimeError("Healthy route snapshot is unavailable")
        self.executor._calibration_current = False
        self.episode_result = {}
        self.episode_result = observe_episode(
            self.executor,
            scenario.episode,
            baseline_window=self.baseline_window,
            result=self.episode_result,
        )
        if catalog_v2:
            from netopsbench.platform.observability.routes import collect_routes

            from .effects import scoped_integrity_errors, validate_effect

            with self.executor.timings.measure(
                "effect_validation",
                timeout_seconds=self.executor.scale_registry.get(scenario.scale).health_timeout_seconds,
            ):
                current = collect_routes(
                    self.executor.current_manifest(),
                    cancel_event=self.executor._cancel_event,
                    deadline=time.monotonic() + self.executor.scale_registry.get(scenario.scale).health_timeout_seconds,
                )
                self.episode_result["route_evidence"] = {"baseline": self.executor._case_routes, "fault": current}
                effect = validate_effect(
                    self.executor, scenario.episode, self.episode_result, self.executor._case_routes, current
                )
                self.episode_result["effect_audit"] = effect
                integrity_errors = scoped_integrity_errors(
                    self.episode_result.get("observations") or {},
                    self.episode_result.get("coverage_audit") or {},
                    effect,
                )
                integrity_errors.extend(effect["errors"])
        else:
            integrity_errors = observation_integrity_errors(self.episode_result.get("observations") or {})
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
        with self.executor.timings.measure_step("tool_queries", action.name) as timing:
            result = method(**action.arguments)
            if not result.success:
                timing["failed_calls"] += 1
        return public_tool_result(result)

    def refresh(self, *, min_seconds: float = 0.0) -> None:
        del min_seconds

    def finish(self, *, broken: bool = False) -> None:
        if self._finished:
            return
        self._finished = True
        if self.scenario is None:
            return
        self.executor._recovering = True
        try:
            with self.executor.timings.measure(
                "recovery", timeout_seconds=self.executor.experiment.recovery_timeout_seconds
            ):
                self.cleanup_result = self.executor._cleanup_after_scenario(
                    self.scenario,
                    self.episode_result,
                    baseline_window=self.baseline_window,
                )
                if self.episode_result is not None and "recovery" in self.cleanup_result:
                    self.episode_result["recovery"] = self.cleanup_result["recovery"]
                if "validated_baseline" in self.cleanup_result:
                    self.baseline_window = dict(self.cleanup_result["validated_baseline"])
                with self.executor.timings.measure_step("recovery", "handoff"):
                    self._publish_baseline()
                current_operation_control().remaining()
                if not self.cleanup_result.get("success", False):
                    raise RuntimeError(f"Scenario cleanup failed: {self.cleanup_result}")
        except BaseException as exc:
            if self.cleanup_result is None:
                self.cleanup_result = {"success": False, "status": "recovery_failed", "errors": [str(exc)]}
            handoff = self.executor._baseline_handoff
            handoff.clear("recovery_failed")
            if self.cleanup_result.get("success", True):
                self.cleanup_result.update(
                    success=False,
                    status="recovery_timeout" if isinstance(exc, TimeoutError) else "recovery_failed",
                )
            self.cleanup_result.update(baseline_handoff=False, baseline_handoff_reason="recovery_failed")
            raise
        finally:
            self.executor._recovering = False

    def _publish_baseline(self) -> None:
        from .baseline import RuntimeScope, scope_changes

        handoff = self.executor._baseline_handoff
        if self.cleanup_result is None or self.scenario is None:
            raise RuntimeError("Cannot publish a baseline before cleanup")
        if self.executor._last_calibration and not self.executor._last_calibration["success"]:
            handoff.clear("calibration_failed")
            self.cleanup_result.update(
                success=False, baseline_handoff=False, baseline_handoff_reason="calibration_failed"
            )
            return
        if self.cleanup_result.get("validation_level") == "state_checked":
            scope = self.executor._baseline_scope(self.scenario)
            restored = self.cleanup_result.pop("restored_scope", self._scope)
            success = bool(self.cleanup_result.get("success")) and scope is not None and scope == restored
            if success:
                handoff.scope = scope
                handoff.ready = True
                handoff.last_reason = "state_verified_baseline_retained"
            else:
                handoff.clear("recovery_identity_changed")
                self.cleanup_result["success"] = False
            self.cleanup_result.update(
                baseline_handoff=success, baseline_handoff_reason=handoff.last_reason, baseline_uses=handoff.uses
            )
            return
        self.cleanup_result.pop("restored_scope", None)
        recovered = self.cleanup_result.get("validated_baseline")
        candidate = recovered
        scope = self.executor._baseline_scope(self.scenario)
        bound = self.cleanup_result.get("validated_baseline_scope", self._scope)
        if not self.cleanup_result.get("success"):
            reason = "recovery_failed"
        elif candidate is None:
            reason = "no_verified_healthy_window"
        elif self._scope is None or scope is None:
            reason = "scope_unavailable"
        elif scope != bound:
            reason = "scope_changed"
        else:
            reason = None
        if reason:
            handoff.clear(reason)
            published = False
        else:
            # A failed incident may still have an independently verified recovery
            # window. Publishing it never changes the preceding case's result.
            published = handoff.publish(
                candidate,
                scope,
                self.scenario.id,
                baseline_window_seconds(self.executor, self.executor.minimum_baseline_seconds),
                verified=True,
            )
        self.cleanup_result.update(
            baseline_handoff=published,
            baseline_handoff_reason=handoff.last_reason,
            baseline_scope_changes=scope_changes(self._scope, scope),
        )
        verified_scope = self.cleanup_result.get("validated_baseline_scope")
        if isinstance(verified_scope, RuntimeScope):
            self.cleanup_result["validated_baseline_scope"] = {"digest": verified_scope.digest}


__all__ = ["ExecutorIncidentBackend"]
