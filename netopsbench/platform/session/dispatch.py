"""Worker-pool execution helpers for SDK sessions."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from netopsbench.platform.runtime.manager import RuntimePool
from netopsbench.platform.session.types import ScenarioExecutionRef, WorkerExecutionContext
from netopsbench.platform.worker.pool import WorkerSpec

logger = logging.getLogger(__name__)


class AgentLike(Protocol):
    name: str

    def diagnose(self, context: Any) -> Any: ...


class EvaluatorLike(Protocol):
    def generate_report(
        self,
        results: list[Any],
        agent_name: str = "unknown",
        topology_scale: str = "unknown",
    ) -> dict[str, Any]: ...


class ScenarioRunnerLike(Protocol):
    results_dir: Path

    def run_scenario(
        self,
        scenario: Any,
        diagnosis_callback: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...


class ScenarioRunnerFactory(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> ScenarioRunnerLike: ...


class ReportSaver(Protocol):
    def __call__(self, report_payload: dict[str, Any], report_path: Path) -> None: ...


class RunHandleBuilder(Protocol):
    def __call__(self, **kwargs: Any) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Configuration object for execute_on_runtime_pool
# ---------------------------------------------------------------------------


@dataclass
class RuntimePoolConfig:
    """Groups the parameters for :func:`execute_on_runtime_pool`."""

    # -- core execution context ---
    run_id: str
    mode: str
    scenarios: list[ScenarioExecutionRef]
    runtime: RuntimePool
    agent: Any

    # -- artifact paths ---
    artifact_dir: Path
    raw_dir: Path
    report_path: Path
    metadata_path: Path

    # -- lifecycle ---
    runtime_owner: str
    teardown: str
    started_at: Any
    completed_at_factory: Callable[[], Any]

    # -- factory callbacks ---
    scenario_runner_cls: Callable[..., ScenarioRunnerLike]
    evaluator_factory: Callable[[], EvaluatorLike]
    score_fault_episodes: Callable[..., list[Any]]
    diagnosis_callback_builder: Callable[
        [Any, str, str, WorkerExecutionContext],
        Callable[[dict[str, Any]], dict[str, Any]],
    ]
    worker_context_builder: Callable[[WorkerSpec, Path], WorkerExecutionContext]
    topology_metadata_loader: Callable[[Path], dict[str, Any] | None]

    # -- reporting callbacks ---
    create_run_report: Callable[..., dict[str, Any]]
    save_run_report: Callable[[dict[str, Any], Path], None]
    save_run_metadata: Callable[..., None]
    build_run_handle: Callable[..., dict[str, Any]]
    run_handle_adapter: Callable[[dict[str, Any]], Any]
    artifact_manager: Any

    # -- scenario executor knobs (defaults preserve historical behaviour) ---
    baseline_wait_seconds: float = 5
    post_recovery_wait_seconds: float = 2
    skip_none_episodes: bool = True


def assign_scenarios_to_workers(
    scenarios: list[ScenarioExecutionRef],
    workers: Sequence[WorkerSpec],
) -> dict[str, list[ScenarioExecutionRef]]:
    if not workers:
        raise ValueError("runtime pool must contain at least one worker")
    assignments: dict[str, list[ScenarioExecutionRef]] = {
        str(worker.id or f"worker-{worker.index}"): [] for worker in workers
    }
    for index, scenario in enumerate(scenarios):
        worker = workers[index % len(workers)]
        worker_id = str(worker.id or f"worker-{worker.index}")
        assignments[worker_id].append(scenario)
    return assignments


def build_scenario_executor(
    pool_config: RuntimePoolConfig,
    worker_context: WorkerExecutionContext,
    worker_raw_dir: Path,
) -> ScenarioRunnerLike:
    """Construct a scenario executor for a single worker.

    Centralising the construction lets callers (and tests) override
    ``pool_config.scenario_runner_cls`` with a stub while keeping the kwargs
    used by the dispatcher in one place. The ``pool_config`` parameter is
    named to avoid shadowing the global :data:`netopsbench.config` singleton.
    """
    kwargs = {
        "topology_dir": str(worker_context.topology_dir),
        "topology_metadata": pool_config.topology_metadata_loader(worker_context.topology_dir),
        "baseline_wait_seconds": pool_config.baseline_wait_seconds,
        "post_recovery_wait_seconds": pool_config.post_recovery_wait_seconds,
        "skip_none_episodes": pool_config.skip_none_episodes,
        "influxdb_bucket": worker_context.influxdb_bucket,
        "topology_id": worker_context.topology_id,
        "persist_results": False,
    }
    try:
        runner = pool_config.scenario_runner_cls(**kwargs)
    except TypeError:
        # Compatibility for test doubles or older runner classes that do not
        # expose the session-owned persistence switch yet.
        kwargs.pop("persist_results", None)
        runner = pool_config.scenario_runner_cls(**kwargs)
    runner.results_dir = worker_raw_dir
    return runner


def _run_worker(
    config: RuntimePoolConfig,
    worker: WorkerSpec,
    worker_scenarios: list[ScenarioExecutionRef],
) -> tuple[list[Any], list[dict[str, Any]], dict[str, Any]]:
    """Execute all scenarios assigned to a single worker.

    Returns ``(eval_results, scenario_summaries, worker_summary)`` without
    touching any shared mutable state so this function is safe to call from
    a thread-pool.
    """
    worker_id = str(worker.id or f"worker-{worker.index}")
    worker_name = str(worker.name or worker_id)
    topology_root = worker.topology_dir or (str(worker.root_dir) if worker.root_dir is not None else None)
    if topology_root is None:
        raise ValueError(f"worker '{worker_id}' is missing topology_dir/root_dir")

    topology_dir = Path(topology_root)
    worker_context = config.worker_context_builder(worker, topology_dir)
    worker_raw_dir = config.raw_dir / worker_name
    worker_raw_dir.mkdir(parents=True, exist_ok=True)

    runner = build_scenario_executor(config, worker_context, worker_raw_dir)
    evaluator = config.evaluator_factory()

    eval_results: list[Any] = []
    scenario_summaries: list[dict[str, Any]] = []
    worker_success = True
    executed_count = 0

    for scenario in worker_scenarios:
        diagnosis_callback = config.diagnosis_callback_builder(
            config.agent,
            str(worker_context.topology_dir),
            scenario.id,
            worker_context,
        )
        scenario_result = runner.run_scenario(
            scenario.to_scenario(),
            diagnosis_callback=diagnosis_callback,
        )
        raw_result_path = _persist_raw_scenario_result(
            worker_raw_dir=worker_raw_dir,
            scenario_id=scenario.id,
            scenario_result=scenario_result,
        )
        scored = config.score_fault_episodes(
            scenario.to_scenario(),
            scenario_result,
            evaluator,
            topology_dir=str(worker_context.topology_dir),
        )
        executed_count += 1
        eval_results.extend(scored)
        scenario_summaries.append(
            {
                "scenario_id": scenario.id,
                "status": "completed" if scenario_result.get("success") else "failed",
                "scale": scenario.scale,
                "worker": worker_name,
                "raw_result_path": raw_result_path,
            }
        )
        if not scenario_result.get("success"):
            worker_success = False

    worker_summary = {
        "worker_id": worker_id,
        "worker_name": worker_name,
        "lab_name": worker.lab_name,
        "scenario_count": len(worker_scenarios),
        "executed_count": executed_count,
        "success": worker_success,
    }
    return eval_results, scenario_summaries, worker_summary


def _persist_raw_scenario_result(
    *,
    worker_raw_dir: Path,
    scenario_id: str,
    scenario_result: dict[str, Any],
) -> str:
    existing = scenario_result.get("result_file")
    if existing:
        result_path = Path(str(existing))
        if result_path.exists():
            return str(result_path)
        result_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        safe_id = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(scenario_id))
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
        result_path = worker_raw_dir / f"{safe_id}_{timestamp}.json"

    result_path.write_text(json.dumps(scenario_result, indent=2, default=str), encoding="utf-8")
    scenario_result["result_file"] = str(result_path)
    return str(result_path)


def execute_on_runtime_pool(
    config: RuntimePoolConfig | None = None,
    **kwargs,
):
    """Execute scenarios across a runtime worker pool.

    Accepts either a :class:`RuntimePoolConfig` instance or keyword arguments
    (for backward compatibility).

    When the pool contains more than one worker the workers execute **in
    parallel** using threads so that each Containerlab topology can run its
    assigned scenarios concurrently.
    """
    if config is None:
        config = RuntimePoolConfig(**kwargs)

    if not config.runtime.workers:
        raise ValueError("runtime must contain at least one worker")

    assignments = assign_scenarios_to_workers(config.scenarios, config.runtime.workers)
    # -- parallel worker execution -----------------------------------------
    num_workers = len(config.runtime.workers)
    all_eval_results: list[Any] = []
    scenario_summaries: list[dict[str, Any]] = []
    worker_summaries: list[dict[str, Any]] = []

    if num_workers == 1:
        # Fast path: single worker, skip thread-pool overhead.
        worker = config.runtime.workers[0]
        wid = str(worker.id or f"worker-{worker.index}")
        evals, sc_sums, w_sum = _run_worker(
            config,
            worker,
            assignments.get(wid, []),
        )
        all_eval_results.extend(evals)
        scenario_summaries.extend(sc_sums)
        worker_summaries.append(w_sum)
    else:
        logger.info("Executing %d workers in parallel", num_workers)
        # Map futures back to workers so results can be merged in
        # deterministic (worker-index) order after all complete.
        futures_map: dict[Any, WorkerSpec] = {}
        with ThreadPoolExecutor(max_workers=num_workers) as pool:
            for worker in config.runtime.workers:
                wid = str(worker.id or f"worker-{worker.index}")
                fut = pool.submit(
                    _run_worker,
                    config,
                    worker,
                    assignments.get(wid, []),
                )
                futures_map[fut] = worker

            # Wait for all to complete; propagate first failure.
            results_by_worker: dict[int, tuple[list[Any], list[dict[str, Any]], dict[str, Any]]] = {}
            for fut in as_completed(futures_map):
                worker = futures_map[fut]
                results_by_worker[worker.index] = fut.result()  # raises on error

        # Merge in stable worker-index order.
        for worker in sorted(config.runtime.workers, key=lambda w: w.index):
            evals, sc_sums, w_sum = results_by_worker[worker.index]
            all_eval_results.extend(evals)
            scenario_summaries.extend(sc_sums)
            worker_summaries.append(w_sum)

    if all_eval_results:
        report_evaluator = config.evaluator_factory()
        aggregate_report = report_evaluator.generate_report(
            all_eval_results,
            agent_name=getattr(config.agent, "name", config.agent.__class__.__name__),
            topology_scale=config.runtime.scale,
        )
    else:
        aggregate_report = {"summary": {"total_cases": 0, "average_score": 0.0}, "detailed_results": []}

    completed_at = config.completed_at_factory()
    report_payload = config.create_run_report(
        run_id=config.run_id,
        mode=config.mode,
        started_at=config.started_at,
        completed_at=completed_at,
        runtime=config.runtime,
        runtime_owner=config.runtime_owner,
        teardown=config.teardown,
        scenarios=config.scenarios,
        agent=config.agent,
        worker_summaries=worker_summaries,
        scenario_summaries=scenario_summaries,
        aggregate_report=aggregate_report,
        artifact_dir=config.artifact_dir,
        raw_dir=config.raw_dir,
        report_path=config.report_path,
        metadata_path=config.metadata_path,
    )
    config.save_run_report(report_payload, config.report_path)
    report_status = str(
        (report_payload.get("raw") or {}).get("status")
        or (report_payload.get("summary") or {}).get("status")
        or report_payload.get("status")
        or "unknown"
    )
    config.save_run_metadata(
        config.artifact_manager,
        config.artifact_dir,
        run_id=config.run_id,
        mode=config.mode,
        status=report_status,
        runtime_id=config.runtime.id,
        runtime_owner=config.runtime_owner,
        teardown=config.teardown,
        started_at=config.started_at,
        completed_at=completed_at,
        scenarios=config.scenarios,
        worker_summaries=worker_summaries,
    )
    handle_payload = config.build_run_handle(
        run_id=config.run_id,
        mode=config.mode,
        status=report_status,
        started_at=config.started_at,
        completed_at=completed_at,
        artifact_dir=config.artifact_dir,
        scenarios=config.scenarios,
        runtime_id=config.runtime.id,
        report_path=config.report_path,
    )
    return config.run_handle_adapter(handle_payload)
