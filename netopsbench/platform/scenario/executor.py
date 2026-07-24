"""Execute one canonical diagnostic scenario."""

from __future__ import annotations

import json
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
from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.faults.scenario_execution import inject_fault as _inject_fault_impl
from netopsbench.platform.faults.scenario_execution import recover_fault as _recover_fault_impl
from netopsbench.platform.faults.specs import FaultSpecRegistry, create_fault_registry
from netopsbench.platform.runtime.health import HEALTH_POLL_INTERVAL_SECONDS
from netopsbench.platform.topology.topology_utils import coerce_topology_manifest, load_topology_manifest
from netopsbench.platform.traffic.controller import TrafficController
from netopsbench.platform.traffic.scenario_execution import setup_traffic as _setup_traffic_impl
from netopsbench.platform.traffic.scenario_execution import stop_traffic as _stop_traffic_impl

from .incident_backend import ExecutorIncidentBackend
from .observation import analyze_observation_windows as _analyze_observation_windows_impl
from .observation import capture_baseline_window as _capture_baseline_window_impl
from .observation import capture_observation_window as _capture_observation_window_impl
from .observation import wait_and_observe as _wait_and_observe_impl

logger = get_logger(__name__)

if TYPE_CHECKING:
    from netopsbench.platform.simulator.engine import DiagnosticSession


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
        minimum_baseline_seconds: int = 60,
        post_recovery_wait_seconds: int = 2,
        influxdb_url: str | None = None,
        influxdb_token: str | None = None,
        influxdb_org: str | None = None,
        influxdb_bucket: str | None = None,
        topology_id: str | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        persist_results: bool = True,
        fault_registry: FaultSpecRegistry | None = None,
        scale_registry: ScaleRegistry | None = None,
        evaluator: Evaluator | None = None,
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
        self.fault_registry = fault_registry or create_fault_registry()
        self.scale_registry = scale_registry or default_scale_registry()
        self.scale_registry.get(manifest.scale)
        self.topology_metadata = manifest.model_dump(mode="json")
        self.injector = FaultInjector(
            clab_dir=topology_dir,
            topology_metadata=self.topology_metadata,
            fault_registry=self.fault_registry,
        )
        self.traffic_controller: TrafficController | None = None
        self.results_dir = Path("scenario_results")
        self.topology_id = topology_id or manifest.topology_id
        self.influxdb_url = influxdb_url
        self.influxdb_token = influxdb_token
        self.influxdb_org = influxdb_org
        self.influxdb_bucket = influxdb_bucket
        self.minimum_baseline_seconds = max(0, int(minimum_baseline_seconds))
        self.post_recovery_wait_seconds = max(0, int(post_recovery_wait_seconds))
        self._sleep_fn = sleep_fn or time.sleep
        self.persist_results = bool(persist_results)
        self.evaluator = evaluator or Evaluator()

    def sleep(self, seconds: float) -> None:
        self._sleep_fn(seconds)

    def _setup_traffic(self, scale: str, profile: str) -> dict:
        return _setup_traffic_impl(self, scale, profile)

    def _stop_traffic(self):
        _stop_traffic_impl(self)

    def close(self) -> None:
        """Release runtime-owned traffic after the worker or simulator is done."""
        self._stop_traffic()

    def _inject_fault(self, episode: EpisodeSpec) -> dict:
        return _inject_fault_impl(self, episode)

    def _wait_and_observe(
        self,
        duration: int,
        *,
        baseline_window: dict,
    ) -> dict:
        return _wait_and_observe_impl(self, duration, baseline_window=baseline_window)

    def _capture_baseline_window(self) -> dict:
        return _capture_baseline_window_impl(self, self.minimum_baseline_seconds)

    def _capture_observation_window(self, duration: int, name: str) -> dict:
        return _capture_observation_window_impl(self, duration, name=name)

    def _recover_fault(self):
        return _recover_fault_impl(self)

    @staticmethod
    def _recovery_results_succeeded(results: object) -> bool:
        if not isinstance(results, list):
            return False
        return all(isinstance(item, dict) and item.get("recovered") is True for item in results)

    def _cleanup_after_scenario(self, scenario: ScenarioSpec, episode_result: dict | None) -> dict:
        """Recover the fault while runtime-owned background traffic remains active."""
        started = monotonic()
        profile = self.scale_registry.get(scenario.topology_scale)
        timeout_seconds = float(profile.health_timeout_seconds)
        deadline = started + timeout_seconds
        attempts = 0
        errors: list[str] = []
        prior_recovery = episode_result.get("recovery") if isinstance(episode_result, dict) else None
        recovery_results = prior_recovery

        while attempts == 0 or monotonic() < deadline:
            attempts += 1
            active_faults = list(getattr(self.injector, "active_faults", []) or [])
            recovery_complete = not active_faults and (
                recovery_results is None or self._recovery_results_succeeded(recovery_results)
            )
            if not recovery_complete:
                try:
                    recovery_results = self._recover_fault()
                    active_faults = list(getattr(self.injector, "active_faults", []) or [])
                    recovery_complete = self._recovery_results_succeeded(recovery_results) and not active_faults
                    if not recovery_complete:
                        errors.append(
                            f"fault_recovery: remaining_faults={len(active_faults)} "
                            f"results={recovery_results!r}"
                        )
                except Exception as exc:  # noqa: BLE001 - bounded retry records the failure
                    recovery_complete = False
                    errors.append(f"fault_recovery: {type(exc).__name__}: {exc}")

            if recovery_complete:
                result = {
                    "success": True,
                    "status": "clean" if attempts == 1 else "recovered_after_retry",
                    "attempts": attempts,
                    "duration_seconds": max(0.0, monotonic() - started),
                    "errors": list(dict.fromkeys(errors)),
                }
                if recovery_results is not None:
                    result["recovery"] = recovery_results
                return result

            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            self.sleep(min(float(HEALTH_POLL_INTERVAL_SECONDS), remaining))

        return {
            "success": False,
            "status": "recovery_timeout",
            "attempts": attempts,
            "duration_seconds": max(0.0, monotonic() - started),
            "timeout_seconds": timeout_seconds,
            "remaining_faults": len(getattr(self.injector, "active_faults", []) or []),
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
        logger.info(f"\n{'#'*70}")
        logger.info(f"# Scenario: {scenario.name}")
        logger.info(f"# ID: {scenario.scenario_id}")
        logger.info(f"# Description: {scenario.description}")
        logger.info(f"# Topology: {scenario.topology_scale}")
        logger.info(f"# Traffic Profile: {scenario.traffic_profile}")
        logger.info("# Episode: 1")
        logger.info(f"{'#'*70}")

        scenario_result = {
            "scenario_id": scenario.scenario_id,
            "name": scenario.name,
            "start_time": datetime.now(UTC).isoformat(),
            "topology_scale": scenario.topology_scale,
            "traffic_profile": scenario.traffic_profile,
            "episode": None,
            "success": False,
        }

        incident = None
        backend = ExecutorIncidentBackend(self)
        try:
            from netopsbench.platform.simulator.engine import (
                FailureDomain,
                IncidentEngine,
                IncidentState,
                TerminationReason,
            )
            from netopsbench.platform.simulator.environment import (
                AgentUsage,
                DiagnosisSubmission,
                SimulatorConfig,
            )

            incident = IncidentEngine(lambda: backend, evaluator=self.evaluator).prepare(scenario)
            scenario_result["traffic_config"] = backend.traffic_config
            scenario_result["episode"] = backend.episode_result
            if incident.state is IncidentState.BROKEN:
                failure = incident.failure
                scenario_result["error"] = failure.message if failure else "incident preparation failed"
                scenario_result["failure"] = failure.model_dump(mode="json") if failure else None
            else:
                episode_result = backend.episode_result or {}
                session = incident.open_session(
                    SimulatorConfig(
                        max_tool_calls=1_000,
                        max_agent_seconds=86_400,
                        max_tool_result_bytes=64 * 1024 * 1024,
                        orphan_lease_ttl_seconds=86_700,
                    )
                )
                if diagnosis_callback is not None:
                    try:
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
                    episode_result["diagnosis"] = diagnosis
                    if not diagnosis or diagnosis.get("error"):
                        transition = session.terminate_failure(
                            domain=FailureDomain.AGENT,
                            message=(diagnosis or {}).get("error", "diagnosis unavailable"),
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
                            transition = session.submit(submission, usage=usage)
                    episode_result["execution"] = transition.model_dump(mode="json")
                    if session.evaluation_result is not None:
                        episode_result["evaluation_result"] = session.evaluation_result
                episode_result["success"] = True
                episode_result["state"] = "terminal"
                scenario_result["success"] = True
        except Exception as e:  # noqa: BLE001 - scenario result records infrastructure failures
            logger.info(f"\n✗ Scenario failed: {e}")
            scenario_result["error"] = str(e)

        finally:
            if incident is not None:
                scenario_result["incident_cleanup_status"] = incident.close().value
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

        if self.persist_results:
            result_file = self._persist_scenario_result(scenario, scenario_result)
            scenario_result["result_file"] = str(result_file)
        else:
            result_file = scenario_result.get("result_file")

        logger.info(f"\n{'#'*70}")
        logger.info("# Scenario Complete")
        logger.info(f"# Success: {scenario_result['success']}")
        if result_file:
            logger.info(f"# Results saved to: {result_file}")
        logger.info(f"{'#'*70}")

        return scenario_result

    def _persist_scenario_result(self, scenario: ScenarioSpec, scenario_result: dict) -> Path:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        result_file = self.results_dir / f"{scenario.scenario_id}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(scenario_result, f, indent=2)
        return result_file
