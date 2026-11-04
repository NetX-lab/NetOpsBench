"""Worker-pool execution for SDK sessions."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from netopsbench.evaluator.fault_type_judge import create_judge_from_env
from netopsbench.evaluator.scorer import Evaluator
from netopsbench.models.profiles import ScaleRegistry
from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.models.scenario import ScenarioSpec
from netopsbench.platform.runtime.manager import RuntimePool
from netopsbench.platform.scenario.artifacts import persist_result
from netopsbench.platform.scenario.executor import ScenarioExecutor
from netopsbench.platform.scenario.validator import require_scenario_topology
from netopsbench.platform.session.context import build_worker_execution_context
from netopsbench.platform.session.diagnosis import build_runtime_diagnosis_callback
from netopsbench.platform.session.reporting import load_topology_metadata
from netopsbench.platform.session.scoring import score_scenario_episode
from netopsbench.platform.session.trace_store import TraceWriter
from netopsbench.platform.session.types import WorkerExecutionContext

logger = logging.getLogger(__name__)

WorkerRunResult = tuple[list[Any], list[dict[str, Any]], dict[str, Any]]


@dataclass
class ExecutionControl:
    """Internal acceptance hooks; ordinary SDK sessions keep their defaults."""

    on_case: Callable[[dict[str, Any], dict[str, Any], list[Any]], None] | None = None
    stop_on_failure: bool = False
    cancel_event: Any = None
    effects_only: bool = False


def is_recovered_case_failure(summary: dict[str, Any]) -> bool:
    """Finished diagnosis or effect failures can advance after verified recovery."""
    return summary.get("cleanup_failed") is False and (
        (
            summary.get("status") == "failed"
            and summary.get("failure_stage") == "agent"
            and summary.get("case_valid") is True
        )
        or (
            summary.get("status") == "invalid"
            and summary.get("failure_stage") == "effect_validation"
            and summary.get("case_valid") is False
        )
    )


@dataclass(frozen=True)
class PoolDispatchResult:
    evaluations: list[Any]
    scenarios: list[dict[str, Any]]
    workers: list[dict[str, Any]]


def assign_scenarios_to_workers(
    scenarios: list[ScenarioSpec],
    workers: Sequence[RuntimeIdentity],
) -> dict[str, list[ScenarioSpec]]:
    if not workers:
        raise ValueError("runtime pool must contain at least one worker")
    assignments: dict[str, list[ScenarioSpec]] = {worker.worker_id: [] for worker in workers}
    for index, scenario in enumerate(scenarios):
        assignments[workers[index % len(workers)].worker_id].append(scenario)
    return assignments


def _dispatch_workers(
    workers: Sequence[RuntimeIdentity],
    scenarios: list[ScenarioSpec],
    run_worker: Callable[[RuntimeIdentity, list[ScenarioSpec]], WorkerRunResult],
) -> PoolDispatchResult:
    if not workers:
        raise ValueError("runtime must contain at least one worker")
    ordered_workers = sorted(workers, key=lambda item: item.worker_index)
    assignments = assign_scenarios_to_workers(scenarios, ordered_workers)

    if len(ordered_workers) == 1:
        worker = ordered_workers[0]
        results = {worker.worker_index: run_worker(worker, assignments[worker.worker_id])}
    else:
        logger.info("Executing %d workers in parallel", len(ordered_workers))
        results = {}
        with ThreadPoolExecutor(max_workers=len(ordered_workers)) as pool:
            futures = {
                pool.submit(run_worker, worker, assignments[worker.worker_id]): worker for worker in ordered_workers
            }
            for future in as_completed(futures):
                worker = futures[future]
                results[worker.worker_index] = future.result()

    evaluations: list[Any] = []
    scenario_summaries: list[dict[str, Any]] = []
    worker_summaries: list[dict[str, Any]] = []
    for worker in ordered_workers:
        worker_evaluations, worker_scenarios, worker_summary = results[worker.worker_index]
        evaluations.extend(worker_evaluations)
        scenario_summaries.extend(worker_scenarios)
        worker_summaries.append(worker_summary)
    return PoolDispatchResult(evaluations, scenario_summaries, worker_summaries)


def _build_scenario_executor(
    worker_context: WorkerExecutionContext,
    *,
    worker: RuntimeIdentity,
    fault_registry: Any,
    minimum_baseline_seconds: int | None,
    scale_registry: ScaleRegistry,
    effects_only: bool = False,
) -> ScenarioExecutor:
    runner = ScenarioExecutor(
        topology_dir=str(worker_context.topology_dir),
        topology_metadata=load_topology_metadata(worker_context.topology_dir),
        minimum_baseline_seconds=minimum_baseline_seconds,
        influxdb_bucket=worker_context.influxdb_bucket,
        topology_id=worker_context.topology_id,
        fault_registry=fault_registry,
        scale_registry=scale_registry,
        evaluator=Evaluator() if effects_only else _create_evaluator(),
        runtime_worker=worker,
    )
    return runner


def _create_evaluator() -> Evaluator:
    judge = create_judge_from_env()
    return Evaluator(fault_type_judge=judge) if judge is not None else Evaluator()


def _run_worker(
    *,
    runtime: RuntimePool,
    agent: Any,
    raw_dir: Path,
    trace_writer: TraceWriter | None,
    fault_registry: Any,
    minimum_baseline_seconds: int | None,
    scale_registry: ScaleRegistry,
    worker: RuntimeIdentity,
    scenarios: list[ScenarioSpec],
    control: ExecutionControl | None = None,
    destroy_after_run: bool = False,
) -> WorkerRunResult:
    worker_context = build_worker_execution_context(worker, worker.topology_dir)
    for scenario in scenarios:
        require_scenario_topology(scenario, str(worker_context.topology_dir))
    worker_raw_dir = raw_dir / worker.worker_id
    worker_raw_dir.mkdir(parents=True, exist_ok=True)
    runner = _build_scenario_executor(
        worker_context,
        worker=worker,
        fault_registry=fault_registry,
        minimum_baseline_seconds=minimum_baseline_seconds,
        scale_registry=scale_registry,
        **({"effects_only": True} if control and control.effects_only else {}),
    )
    evaluator = runner.evaluator
    if control:
        runner._cancel_event = control.cancel_event

    evaluations: list[Any] = []
    scenario_summaries: list[dict[str, Any]] = []
    worker_success = True

    executed_count = 0
    final_calibration = None
    try:
        for scenario_index, scenario in enumerate(scenarios):
            if control and control.cancel_event and control.cancel_event.is_set():
                worker_success = False
                break
            callback = (
                None
                if control and control.effects_only
                else build_runtime_diagnosis_callback(
                    agent,
                    str(worker_context.topology_dir),
                    scenario.id,
                    worker_context,
                    trace_writer,
                    worker.worker_id,
                    runtime.id,
                    scenario.scale,
                    cancel_event=control.cancel_event if control else None,
                )
            )
            scenario_result = runner.run_scenario(scenario, diagnosis_callback=callback)
            executed_count += 1
            raw_result_path = _persist_raw_scenario_result(worker_raw_dir, scenario.id, scenario_result)
            case_valid = bool(scenario_result.get("case_valid", scenario_result.get("success")))
            scored: list[Any] = []
            if case_valid and not (control and control.effects_only):
                try:
                    scored = score_scenario_episode(
                        scenario,
                        scenario_result,
                        evaluator,
                        topology_dir=str(worker_context.topology_dir),
                    )
                except Exception as exc:
                    if trace_writer is not None:
                        try:
                            trace_writer.write_failure_result(
                                scenario_id=scenario.id,
                                scenario_result=scenario_result,
                                stage="evaluator",
                                error=exc,
                            )
                        except Exception:
                            logger.debug("failed to persist evaluator failure trace result", exc_info=True)
                    raise
                if trace_writer is not None:
                    try:
                        trace_writer.write_evaluation_results(
                            evaluation_results=scored,
                            scenario_result=scenario_result,
                        )
                    except Exception:
                        logger.debug("failed to persist trace evaluation results", exc_info=True)
            evaluations.extend(scored)
            cleanup_success = bool((scenario_result.get("cleanup") or {}).get("success", True))
            episode_result = scenario_result.get("episode") or {}
            execution_failure = (episode_result.get("execution") or {}).get("failure") or {}
            diagnosis_metadata = (episode_result.get("diagnosis") or {}).get("metadata") or {}
            recovered = (scenario_result.get("cleanup") or {}).get("success") is True and not (
                control and control.cancel_event and control.cancel_event.is_set()
            )
            recovered_agent_failure = (
                case_valid
                and not scenario_result.get("error")
                and execution_failure.get("domain") in {"agent", "protocol"}
                and execution_failure.get("phase") in {"diagnosis", "limits"}
                and not diagnosis_metadata.get("cleanup_errors")
                and recovered
            )
            recovered_effect_failure = (
                not case_valid
                and not (control and control.effects_only)
                and (episode_result.get("effect_audit") or {}).get("valid") is False
                and not episode_result.get("diagnosis")
                and bool((scenario_result.get("cleanup") or {}).get("validated_baseline"))
                and recovered
            )
            success = case_valid and bool(scenario_result.get("success")) and cleanup_success
            if not case_valid:
                status = "invalid"
                failure_stage = "effect_validation" if recovered_effect_failure else "infrastructure"
            elif success:
                status = "completed"
                failure_stage = None
            else:
                status = "failed"
                if not cleanup_success:
                    failure_stage = "cleanup"
                else:
                    failure_stage = "agent" if recovered_agent_failure else "execution"
            scenario_summaries.append(
                {
                    "scenario_id": scenario.id,
                    "status": status,
                    "case_valid": case_valid,
                    "scale": scenario.scale,
                    "worker": worker.worker_id,
                    "raw_result_path": raw_result_path,
                    "cleanup_failed": not cleanup_success,
                    "phase_timings": scenario_result.get("phase_timings", {}),
                    **({"failure_stage": failure_stage} if failure_stage else {}),
                }
            )
            worker_success &= success
            if not cleanup_success:
                runtime.mark_quarantined("scenario_cleanup_failure", [worker.worker_id])
            if control and control.on_case:
                control.on_case(scenario_summaries[-1], scenario_result, scored)
            if not cleanup_success or (
                control
                and control.stop_on_failure
                and not success
                and not is_recovered_case_failure(scenario_summaries[-1])
            ):
                for skipped in scenarios[scenario_index + 1 :]:
                    scenario_summaries.append(
                        {
                            "scenario_id": skipped.id,
                            "status": (
                                "skipped_infrastructure_failure" if not cleanup_success else "skipped_execution_failure"
                            ),
                            "scale": skipped.scale,
                            "worker": worker.worker_id,
                            "failure_stage": "prior_case_cleanup" if not cleanup_success else "prior_case_execution",
                        }
                    )
                break
        if executed_count and not any(item.get("cleanup_failed") for item in scenario_summaries):
            try:
                final_calibration = runner.final_calibration(scenario, destroy_after_run=destroy_after_run)
            except BaseException as exc:
                worker_success = False
                final_calibration = {"success": False, "error": str(exc)}
                runtime.mark_quarantined("final_calibration_failure", [worker.worker_id])
                if not isinstance(exc, Exception):
                    persist_result(worker_raw_dir / "final_calibration.json", final_calibration)
                    raise
            persist_result(worker_raw_dir / "final_calibration.json", final_calibration)
    finally:
        runner.close()

    worker_summary = {
        "worker_id": worker.worker_id,
        "worker_name": worker.worker_id,
        "lab_name": worker.lab_name,
        "scenario_count": len(scenarios),
        "executed_count": executed_count,
        "skipped_count": len(scenarios) - executed_count,
        "success": worker_success,
        **({"final_calibration": final_calibration} if final_calibration is not None else {}),
    }
    return evaluations, scenario_summaries, worker_summary


def _persist_raw_scenario_result(
    worker_raw_dir: Path,
    scenario_id: str,
    scenario_result: dict[str, Any],
) -> str:
    safe_id = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(scenario_id))
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    result_path = worker_raw_dir / f"{safe_id}_{timestamp}.json"
    persist_result(result_path, scenario_result)
    scenario_result["result_file"] = str(result_path)
    return str(result_path)


def execute_on_runtime_pool(
    *,
    runtime: RuntimePool,
    scenarios: list[ScenarioSpec],
    agent: Any,
    raw_dir: Path,
    trace_writer: TraceWriter | None = None,
    fault_registry: Any = None,
    minimum_baseline_seconds: int | None = None,
    control: ExecutionControl | None = None,
    destroy_after_run: bool = False,
) -> PoolDispatchResult:
    """Run scenarios on an existing runtime and return ordered execution data."""
    from netopsbench.models.recovery import RecoveryPolicy

    for scenario in scenarios:
        RecoveryPolicy.from_metadata(scenario.metadata)
    if runtime.state != "warm" or bool(runtime.metadata.get("quarantined")):
        raise RuntimeError(
            f"Runtime {runtime.id!r} is not eligible for execution: "
            f"state={runtime.state!r}, quarantined={bool(runtime.metadata.get('quarantined'))}"
        )
    mismatched = [scenario.id for scenario in scenarios if scenario.scale != runtime.scale]
    if mismatched:
        raise ValueError(f"Scenario scale does not match runtime scale {runtime.scale!r}: {', '.join(mismatched)}")
    result = _dispatch_workers(
        runtime.workers,
        scenarios,
        lambda worker, assigned: _run_worker(
            runtime=runtime,
            agent=agent,
            raw_dir=raw_dir,
            trace_writer=trace_writer,
            fault_registry=fault_registry,
            minimum_baseline_seconds=minimum_baseline_seconds,
            scale_registry=runtime.scale_registry,
            worker=worker,
            scenarios=assigned,
            control=control,
            destroy_after_run=destroy_after_run,
        ),
    )
    cleanup_failures = sorted(
        {str(item["worker"]) for item in result.scenarios if item.get("cleanup_failed") is True and item.get("worker")}
    )
    if cleanup_failures:
        runtime.mark_quarantined("scenario_cleanup_failure", cleanup_failures)
    return result


__all__ = ["PoolDispatchResult", "assign_scenarios_to_workers", "execute_on_runtime_pool"]
