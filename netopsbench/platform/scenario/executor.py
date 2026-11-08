"""Execute one canonical diagnostic scenario."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ValidationError

from netopsbench.evaluator.scorer import Evaluator
from netopsbench.logging_utils import get_logger
from netopsbench.models.profiles import ScaleRegistry, default_scale_registry
from netopsbench.models.recovery import RecoveryPolicy
from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.faults.scenario_execution import inject_fault as _inject_fault_impl
from netopsbench.platform.faults.scenario_execution import recover_fault as _recover_fault_impl
from netopsbench.platform.faults.specs import FaultSpecRegistry, create_fault_registry
from netopsbench.platform.runtime.health import (
    HEALTH_POLL_INTERVAL_SECONDS,
    check_worker_health,
)
from netopsbench.platform.topology.topology_utils import coerce_topology_manifest, load_topology_manifest
from netopsbench.platform.traffic.controller import TrafficController
from netopsbench.platform.traffic.scenario_execution import setup_traffic as _setup_traffic_impl
from netopsbench.platform.traffic.scenario_execution import stop_traffic as _stop_traffic_impl
from netopsbench.platform.utils.control import OperationControl, current_operation_control, operation_scope
from netopsbench.platform.utils.measurements import count_operation

from .baseline import BaselineHandoff, RuntimeScope, frr_snapshot, restored_scope_errors, runtime_scope, scope_changes
from .incident_backend import ExecutorIncidentBackend
from .observation import analyze_observation_windows as _analyze_observation_windows_impl
from .observation import capture_baseline_window as _capture_baseline_window_impl
from .observation import capture_observation_window as _capture_observation_window_impl
from .timing import PhaseTimings

logger = get_logger(__name__)

if TYPE_CHECKING:
    from netopsbench.platform.incident.engine import DiagnosticSession


class DiagnosisCallback(Protocol):
    def __call__(
        self,
        episode_result: dict[str, Any],
        *,
        diagnostic_session: DiagnosticSession,
        diagnostic_payload: dict[str, Any],
    ) -> dict[str, Any]: ...


class ScenarioExecutor:
    """
    Orchestrates execution of test scenarios with automated fault injection.
    """

    def __init__(
        self,
        topology_dir: str = "clab-topology",
        topology_metadata: dict | None = None,
        minimum_baseline_seconds: int | None = None,
        influxdb_url: str | None = None,
        influxdb_token: str | None = None,
        influxdb_org: str | None = None,
        influxdb_bucket: str | None = None,
        topology_id: str | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        fault_registry: FaultSpecRegistry | None = None,
        scale_registry: ScaleRegistry | None = None,
        evaluator: Evaluator | None = None,
        runtime_worker: RuntimeIdentity | None = None,
    ):
        """
        Initialize scenario runner.

        Args:
            topology_dir: Directory containing topology files
        """
        self.topology_dir = topology_dir
        metadata = topology_metadata
        if metadata is None:
            topology_file = Path(topology_dir) / "topology.json"
            if topology_file.exists():
                metadata = load_topology_manifest(topology_file).model_dump(mode="json")
        if metadata is None:
            raise ValueError(f"Canonical topology metadata is required for ScenarioExecutor: {topology_dir}")
        manifest = coerce_topology_manifest(metadata)
        self.manifest = manifest
        self._manifest_file_stamp = self._topology_file_stamp()
        self._detector = None
        self._detector_scope = None
        self.fault_registry = fault_registry or create_fault_registry()
        self.scale_registry = scale_registry or default_scale_registry()
        self.experiment = self.scale_registry.get(manifest.scale).experiment
        self.topology_metadata = manifest.model_dump(mode="json")
        self.injector = FaultInjector(
            clab_dir=topology_dir,
            topology_metadata=self.topology_metadata,
            fault_registry=self.fault_registry,
            experiment=self.experiment,
        )
        self.traffic_controller: TrafficController | None = None
        self.topology_id = topology_id or manifest.topology_id
        self.influxdb_url = influxdb_url
        self.influxdb_token = influxdb_token
        self.influxdb_org = influxdb_org
        self.influxdb_bucket = influxdb_bucket
        self.observation_timeout_seconds = self.scale_registry.get(manifest.scale).health_timeout_seconds
        self.minimum_baseline_seconds = (
            self.experiment.observation_seconds
            if minimum_baseline_seconds is None
            else max(0, int(minimum_baseline_seconds))
        )
        self._sleep_fn = sleep_fn or time.sleep
        self._cancel_event = None
        self._recovering = False
        self.evaluator = evaluator or Evaluator()
        self.runtime_worker = runtime_worker
        self._baseline_handoff = BaselineHandoff()
        self._candidate_paths = None
        self._candidate_scope = None
        self._traffic_config = None
        self._pingmesh_runtime_start = None
        self.recovery_policy = RecoveryPolicy()
        self._last_calibration = None
        self._calibration_current = False
        self.begin_case()

    def begin_case(self) -> None:
        """Reset per-case state without invalidating runtime traffic or baseline handoff."""
        self.timings = PhaseTimings()
        self._case_frr_before = None
        self._case_initial_scope = None
        self._case_routes = None
        self._case_affected_pairs = []
        self._case_probe_pairs = []

    def _topology_file_stamp(self):
        path = Path(self.topology_dir) / "topology.json"
        if not path.exists():
            return None
        stat = path.stat()
        return stat.st_ino, stat.st_mtime_ns, stat.st_size

    def current_manifest(self):
        """Reload changed topology artifacts without reparsing every window."""
        stamp = self._topology_file_stamp()
        if stamp != self._manifest_file_stamp:
            manifest = load_topology_manifest(self.topology_dir)
            if manifest.topology_id != self.topology_id:
                raise ValueError("Executor topology identity changed; create a new executor")
            self.manifest = manifest
            self.topology_metadata = manifest.model_dump(mode="json")
            self._manifest_file_stamp = stamp
            self._baseline_handoff.clear()
        return self.manifest

    def sleep(self, seconds: float) -> None:
        self._check_cancelled()
        self._sleep_fn(seconds)
        self._check_cancelled()

    def _check_cancelled(self) -> None:
        if not self._recovering and self._cancel_event is not None and self._cancel_event.is_set():
            raise RuntimeError("scenario cancelled")

    def _setup_traffic(self, scale: str, profile: str) -> dict:
        self._traffic_config = _setup_traffic_impl(self, scale, profile)
        return self._traffic_config

    def _stop_traffic(self):
        _stop_traffic_impl(self)

    def close(self) -> None:
        """Release runtime-owned traffic after the worker or simulator is done."""
        self._baseline_handoff.clear()
        self._stop_traffic()

    def _baseline_scope(self, scenario: ScenarioSpec) -> RuntimeScope | None:
        try:
            return runtime_scope(self, scenario)
        except Exception as exc:
            logger.warning("Baseline handoff disabled: %s", type(exc).__name__)
            return None

    def _inject_fault(self, episode: EpisodeSpec) -> dict:
        self._check_cancelled()
        self._case_frr_before = None
        if self.runtime_worker is not None and (
            episode.fault_type.startswith(("bgp_", "route_"))
            or episode.fault_type
            in {"ecmp_misconfig", "routing_loop", "static_route_misconfig", "blackhole_route", "device_down"}
        ):
            self._case_frr_before = frr_snapshot(self, episode.target_device)
        return _inject_fault_impl(self, episode)

    def _capture_baseline_window(self) -> dict:
        return _capture_baseline_window_impl(self, self.minimum_baseline_seconds)

    def _capture_observation_window(self, duration: int, name: str) -> dict:
        return _capture_observation_window_impl(self, duration, name=name)

    def calibrate(self, scenario, reason, *, deadline=None):
        """Full environment and data-plane validation; independent of fault undo."""
        from .observation import baseline_gate_errors

        if self.injector.active_faults:
            raise RuntimeError("Cannot calibrate a runtime with unresolved faults")
        self._last_calibration = {"success": False, "reason": reason}
        self._calibration_current = False
        deadline = deadline or monotonic() + self.experiment.recovery_timeout_seconds
        phase = "calibration"
        sequence = 1
        while phase in self.timings.phases:
            sequence += 1
            phase = f"calibration_{sequence}"
        with (
            self.timings.measure(phase) as measurement,
            operation_scope(OperationControl(None if self._recovering else self._cancel_event, deadline)),
        ):
            measurement["reason"] = reason
            with self.timings.measure_step(phase, "environment"):
                if self.runtime_worker is not None:
                    errors = check_worker_health(
                        self.runtime_worker,
                        scale_registry=self.scale_registry,
                        all_routing_devices=True,
                        deadline=deadline,
                        route_snapshot={"nodes": {}},
                        expected_routes=self._case_routes,
                    )
                    if errors:
                        raise RuntimeError("Calibration environment failed: " + "; ".join(errors))
            before = self._baseline_scope(scenario)
            with self.timings.measure_step(phase, "window_wait"):
                window = self._capture_baseline_window()
            with self.timings.measure_step(phase, "window_query") as query_step:
                observation = self._merge_observation_windows(
                    [window],
                    int(window["duration_seconds"]),
                    baseline_window=(self._baseline_handoff.window if before == self._baseline_handoff.scope else None)
                    or window,
                )
                query_step["ingestion"] = {
                    "baseline": (observation.get("_baseline_coverage") or {}).get("ingestion", {}),
                    "current": (observation.get("_coverage_audit") or {}).get("ingestion", {}),
                }
            errors = baseline_gate_errors(observation)
            absolute = observation.get("_baseline_health") or {}
            required = {"absolute_unreachable_paths", "absolute_packet_loss_paths", "absolute_network_mtu_paths"}
            if not required <= absolute.keys():
                errors.append("missing_absolute_health")
            after = self._baseline_scope(scenario)
            if errors:
                measurement["failed_validation"] = observation
            if errors or (self.runtime_worker is not None and (before is None or after != before)):
                raise RuntimeError("Calibration failed: " + "; ".join(errors or ["runtime identity changed"]))
            current_operation_control().remaining()
            handoff = self._baseline_handoff
            handoff.publish(window, after, scenario.id, self.minimum_baseline_seconds, verified=True)
            if self.runtime_worker is not None and not handoff.ready:
                raise RuntimeError("Calibration baseline could not be published")
            self._last_calibration = {
                "success": True,
                "reason": reason,
                "window": dict(window),
                "validation_level": "full_window",
                "scope": after.digest if after else None,
                "phase_timing": measurement,
            }
            self._calibration_current = True
            return dict(window)

    def final_calibration(self, scenario, *, destroy_after_run=False):
        if self.injector.active_faults:
            raise RuntimeError("Cannot finish a runtime with unresolved faults")
        if destroy_after_run:
            return {"status": "skipped", "reason": "runtime_will_be_destroyed", "validation_level": "state_checked"}
        handoff = self._baseline_handoff
        scope = self._baseline_scope(scenario)
        from .baseline import window_error

        if (
            self._calibration_current
            and handoff.ready
            and scope == handoff.scope
            and window_error(handoff.window, self.minimum_baseline_seconds) is None
        ):
            return {**self._last_calibration, "reused": True}
        try:
            self.calibrate(scenario, "run_end")
        except BaseException:
            handoff.clear("final_calibration_failed")
            raise
        return self._last_calibration

    def _recover_fault(self):
        return _recover_fault_impl(self)

    @staticmethod
    def _recovery_results_succeeded(results: object) -> bool:
        if not isinstance(results, list):
            return False
        return all(isinstance(item, dict) and item.get("recovered") is True for item in results)

    @staticmethod
    def _recovery_has_terminal_failure(results: object) -> bool:
        return isinstance(results, list) and any(
            isinstance(item, dict) and item.get("recovered") is not True and item.get("retryable") is False
            for item in results
        )

    def _cleanup_after_scenario(
        self,
        scenario: ScenarioSpec,
        episode_result: dict | None,
        *,
        baseline_window: dict[str, Any] | None = None,
    ) -> dict:
        """Recover the fault while runtime-owned background traffic remains active."""
        started = monotonic()
        profile = self.scale_registry.get(scenario.topology_scale)
        timeout_seconds = float(profile.experiment.recovery_timeout_seconds)
        deadline = started + timeout_seconds
        attempts = 0
        errors: list[str] = []
        recovery_attempted = False
        convergence_pending = False
        terminal_recovery_failure = False
        state_audit = {}
        full_validation_reason = None
        prior_recovery = episode_result.get("recovery") if isinstance(episode_result, dict) else None
        recovery_results = prior_recovery

        while attempts == 0 or monotonic() < deadline:
            attempts += 1
            count_operation("recovery_attempts")
            active_faults = list(self.injector.active_faults or [])
            recovery_complete = not active_faults and (
                recovery_results is None or self._recovery_results_succeeded(recovery_results)
            )
            if not recovery_complete:
                try:
                    recovery_attempted = True
                    with (
                        self.timings.measure_step("recovery", "fault_restore"),
                        operation_scope(OperationControl(deadline=deadline)),
                    ):
                        recovery_results = self._recover_fault()
                    active_faults = list(self.injector.active_faults or [])
                    recovery_complete = self._recovery_results_succeeded(recovery_results) and not active_faults
                    if not recovery_complete:
                        errors.append(
                            f"fault_recovery: remaining_faults={len(active_faults)} results={recovery_results!r}"
                        )
                        terminal_recovery_failure = self._recovery_has_terminal_failure(recovery_results)
                except Exception as exc:  # noqa: BLE001 - bounded retry records the failure
                    recovery_complete = False
                    errors.append(f"fault_recovery: {type(exc).__name__}: {exc}")

            if recovery_complete:
                convergence_errors: list[str] = []
                controller = self.traffic_controller
                if controller is not None:
                    with self.timings.measure_step("recovery", "traffic_check"):
                        if not bool(controller.active_flows) or not controller.verify_active_flows(
                            control=OperationControl(deadline=deadline)
                        ):
                            convergence_errors.append("background traffic is not fully ready")
                runtime_worker = self.runtime_worker
                if runtime_worker is not None:
                    route_snapshot = {"nodes": {}}
                    with self.timings.measure_step("recovery", "health_check"):
                        convergence_errors.extend(
                            check_worker_health(
                                runtime_worker,
                                scale_registry=self.scale_registry,
                                all_routing_devices=True,
                                deadline=deadline,
                                route_snapshot=route_snapshot,
                                expected_routes=self._case_routes,
                                state_only=True,
                                recovery_audit=state_audit,
                            )
                        )
                if convergence_errors:
                    convergence_pending = True
                    errors.extend(f"post_recovery: {error}" for error in convergence_errors)
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        break
                    with self.timings.measure_step("recovery", "retry_wait"):
                        self.sleep(min(float(HEALTH_POLL_INTERVAL_SECONDS), remaining))
                    continue
                validated_baseline = None
                verified_scope = None
                config_proof = None
                verified_callbacks = all(item.get("readback_verified") is True for item in (recovery_results or []))
                full_validation_reason = (
                    "custom_callback"
                    if not verified_callbacks
                    else "recovery_retried" if attempts > 1 or state_audit.get("had_errors") else None
                )
                if (recovery_attempted or full_validation_reason) and baseline_window is not None:
                    try:
                        with self.timings.measure_step("recovery", "configuration_identity"):
                            if self._case_frr_before is not None:
                                before = self._case_frr_before
                                after = frr_snapshot(self, before["device"])
                                restored = before["running"] == after["running"] and (
                                    before["saved_bytes"] == after["saved_bytes"] or after["saved"] == after["running"]
                                )
                                config_proof = {"before": before, "after": after, "restored": restored}
                                if not restored:
                                    return {
                                        "success": False,
                                        "status": "configuration_recovery_failed",
                                        "configuration_recovery": config_proof,
                                    }
                            window_scope = self._baseline_scope(scenario) if runtime_worker is not None else None
                            if runtime_worker is not None and self._case_initial_scope is not None:
                                changed = restored_scope_errors(
                                    self._case_initial_scope, window_scope, scenario, config_proof
                                )
                                if changed:
                                    return {
                                        "success": False,
                                        "status": "recovery_identity_changed",
                                        "scope_changes": changed,
                                    }
                        if full_validation_reason:
                            self.calibrate(scenario, full_validation_reason, deadline=deadline)
                            validated_baseline = dict(self._baseline_handoff.window)
                        if monotonic() >= deadline:
                            raise TimeoutError("Recovery deadline exceeded during observation validation")
                        if runtime_worker is not None and window_scope is not None:
                            with self.timings.measure_step("recovery", "configuration_identity"):
                                verified_scope = self._baseline_scope(scenario)
                                if verified_scope is None or scope_changes(window_scope, verified_scope):
                                    return {
                                        "success": False,
                                        "status": "recovery_identity_changed",
                                        "scope_changes": scope_changes(window_scope, verified_scope),
                                    }
                    except TimeoutError as exc:
                        errors.append(f"post_recovery_baseline: {exc}")
                        return {
                            "success": False,
                            "status": "recovery_timeout",
                            "attempts": attempts,
                            "duration_seconds": max(0.0, monotonic() - started),
                            "errors": list(dict.fromkeys(errors)),
                        }
                if monotonic() >= deadline:
                    errors.append("Recovery deadline exceeded")
                    break
                result: dict[str, Any] = {
                    "success": True,
                    "status": "clean" if attempts == 1 else "recovered_after_retry",
                    "attempts": attempts,
                    "duration_seconds": max(0.0, monotonic() - started),
                    "errors": list(dict.fromkeys(errors)),
                    "validation_level": ("state_checked" if not full_validation_reason else "full_window"),
                    "full_validation_reason": full_validation_reason,
                    "state_validation": state_audit,
                    "recovery_policy": self.recovery_policy.model_dump(mode="json"),
                }
                if recovery_results is not None:
                    result["recovery"] = recovery_results
                if validated_baseline is not None:
                    result["validated_baseline"] = validated_baseline
                    if verified_scope is not None:
                        result["validated_baseline_scope"] = verified_scope
                if verified_scope is not None:
                    result["restored_scope"] = verified_scope
                if config_proof is not None:
                    result["configuration_recovery"] = config_proof
                return result

            if terminal_recovery_failure:
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            with self.timings.measure_step("recovery", "retry_wait"):
                self.sleep(min(float(HEALTH_POLL_INTERVAL_SECONDS), remaining))

        return {
            "success": False,
            "status": (
                "terminal_recovery_failure"
                if terminal_recovery_failure
                else ("post_recovery_convergence_timeout" if convergence_pending else "recovery_timeout")
            ),
            "attempts": attempts,
            "duration_seconds": max(0.0, monotonic() - started),
            "timeout_seconds": timeout_seconds,
            "remaining_faults": len(self.injector.active_faults or []),
            "errors": list(dict.fromkeys(errors)),
        }

    def _merge_observation_windows(
        self,
        windows: list[dict],
        total_duration_seconds: int,
        *,
        baseline_window: dict,
    ) -> dict:
        return _analyze_observation_windows_impl(
            self,
            windows,
            total_duration_seconds,
            baseline_window=baseline_window,
        )

    def run_scenario(
        self,
        scenario: ScenarioSpec,
        diagnosis_callback: DiagnosisCallback | None = None,
    ) -> dict:
        """
        Run one canonical diagnostic scenario.

        Args:
            scenario: Scenario specification

        Returns:
            Scenario result dict
        """
        logger.info(f"\n{'#' * 70}")
        logger.info(f"# Scenario: {scenario.name}")
        logger.info(f"# ID: {scenario.scenario_id}")
        logger.info(f"# Description: {scenario.description}")
        logger.info(f"# Topology: {scenario.topology_scale}")
        logger.info(f"# Traffic Profile: {scenario.traffic_profile}")
        logger.info("# Episode: 1")
        logger.info(f"{'#' * 70}")

        self.begin_case()
        case_started = monotonic()
        scenario_result: dict[str, Any] = {
            "fault_catalog_version": scenario.metadata.get("fault_catalog_version"),
            "scenario_id": scenario.scenario_id,
            "name": scenario.name,
            "start_time": datetime.now(UTC).isoformat(),
            "topology_scale": scenario.topology_scale,
            "traffic_profile": scenario.traffic_profile,
            "episode": None,
            "case_valid": False,
            "success": False,
            "phase_timings": self.timings.phases,
        }

        incident = None
        backend = ExecutorIncidentBackend(self)
        try:
            from netopsbench.platform.incident.contracts import (
                AgentUsage,
                DiagnosisSubmission,
                SimulatorConfig,
            )
            from netopsbench.platform.incident.engine import (
                FailureDomain,
                IncidentEngine,
                IncidentState,
                TerminationReason,
            )

            incident = IncidentEngine(lambda: backend, evaluator=self.evaluator).prepare(scenario)
            scenario_result["baseline_preparation"] = backend.baseline_preparation
            scenario_result["traffic_config"] = backend.traffic_config
            scenario_result["episode"] = backend.episode_result
            if incident.state is IncidentState.BROKEN:
                failure = incident.failure
                scenario_result["error"] = failure.message if failure else "incident preparation failed"
                scenario_result["failure"] = failure.model_dump(mode="json") if failure else None
            else:
                scenario_result["case_valid"] = True
                episode_result = backend.episode_result or {}
                if diagnosis_callback is not None:
                    session = incident.open_session(
                        SimulatorConfig(
                            max_tool_calls=1_000,
                            max_agent_seconds=self.experiment.diagnosis_timeout_seconds,
                            max_tool_result_bytes=64 * 1024 * 1024,
                            orphan_lease_ttl_seconds=self.experiment.diagnosis_timeout_seconds + 300,
                        )
                    )
                    session.phase_timings = self.timings
                    try:
                        with self.timings.measure(
                            "diagnosis", timeout_seconds=self.experiment.diagnosis_timeout_seconds
                        ):
                            diagnosis = diagnosis_callback(
                                episode_result,
                                diagnostic_session=session,
                                diagnostic_payload={
                                    "case_id": incident.case_id,
                                    "topology": backend.topology,
                                    "symptoms": backend.symptoms,
                                    "canonical_observation": incident.observation,
                                },
                            )
                    except Exception as exc:  # noqa: BLE001 - agent errors are scored outcomes
                        diagnosis = {
                            "error": str(exc),
                            "success": False,
                            "metadata": {
                                "agent_failure_stage": "diagnose",
                                "error_type": type(exc).__name__,
                            },
                        }
                    cleanup_time = self.timings.phases.get("agent_cleanup", {}).get("actual_seconds", 0)
                    self.timings.phases["diagnosis"]["actual_seconds"] = max(
                        0, self.timings.phases["diagnosis"]["actual_seconds"] - cleanup_time
                    )
                    episode_result["diagnosis"] = diagnosis
                    if not diagnosis or diagnosis.get("error") or diagnosis.get("success") is False:
                        self.timings.phases["diagnosis"]["status"] = "failed"
                        transition = session.terminate_failure(
                            domain=FailureDomain.AGENT,
                            message=(diagnosis or {}).get("error") or "diagnosis unavailable",
                            reason=TerminationReason.AGENT_ERROR,
                        )
                    else:
                        metadata = diagnosis.get("metadata") or {}
                        usage = AgentUsage(
                            input_tokens=int(metadata.get("input_tokens", 0) or 0),
                            output_tokens=int(metadata.get("output_tokens", 0) or 0),
                        )
                        try:
                            submission = DiagnosisSubmission.model_validate(
                                {
                                    "verdict": diagnosis.get("verdict", "inconclusive"),
                                    "fault_type": diagnosis.get("fault_type"),
                                    "location": diagnosis.get("location") or {},
                                    "evidence": diagnosis.get("evidence") or [],
                                    "confidence": diagnosis.get("confidence", 0.0),
                                    "reasoning": diagnosis.get("reasoning", ""),
                                }
                            )
                        except ValidationError as exc:
                            transition = session.terminate_failure(
                                domain=FailureDomain.PROTOCOL,
                                message=str(exc),
                                reason=TerminationReason.PROTOCOL_ERROR,
                            )
                        else:
                            transition = session.submit(
                                submission,
                                usage=usage,
                                tool_calls=list(diagnosis.get("tool_calls") or []) or None,
                                time_taken_seconds=float(diagnosis.get("time_taken_seconds", 0.0) or 0.0),
                                metadata=metadata,
                            )
                    episode_result["execution"] = transition.model_dump(mode="json")
                    if session.evaluation_result is not None:
                        episode_result["evaluation_result"] = session.evaluation_result
                execution_success = not (episode_result.get("execution") or {}).get("failure")
                episode_result["success"] = execution_success
                episode_result["state"] = "terminal"
                scenario_result["success"] = execution_success
                if diagnosis_callback is None:
                    self.timings.skip("diagnosis", "effects mode")
        except Exception as e:  # noqa: BLE001 - scenario result records infrastructure failures
            logger.info(f"\n✗ Scenario failed: {e}")
            scenario_result["error"] = str(e)

        finally:
            if incident is not None:
                scenario_result["incident_cleanup_status"] = incident.close().value
                if incident.cleanup_failure is not None:
                    scenario_result["cleanup_failure"] = incident.cleanup_failure.model_dump(mode="json")
            cleanup = backend.cleanup_result
            if cleanup is not None:
                scenario_result["cleanup"] = cleanup
                if not cleanup["success"]:
                    scenario_result["cleanup_failed"] = True
            elif incident is not None:
                cleanup_success = incident.cleanup_status.value == "succeeded"
                scenario_result["cleanup"] = {
                    "success": cleanup_success,
                    "status": incident.cleanup_status.value,
                }
                if not cleanup_success:
                    scenario_result["cleanup_failed"] = True

            scenario_result["end_time"] = datetime.now(UTC).isoformat()
            for phase in (
                "baseline",
                "baseline_query",
                "baseline_routes",
                "traffic_readiness",
                "injection",
                "observation",
                "observation_query",
                "effect_validation",
                "diagnosis",
                "agent_cleanup",
                "recovery",
            ):
                if phase not in self.timings.phases:
                    self.timings.skip(phase, "not reached or not applicable")
            self.timings.phases["total"] = {
                "status": "completed" if scenario_result["success"] else "failed",
                "actual_seconds": monotonic() - case_started,
            }

        logger.info(f"\n{'#' * 70}")
        logger.info("# Scenario Complete")
        logger.info(f"# Success: {scenario_result['success']}")
        logger.info(f"{'#' * 70}")

        return scenario_result
