"""Platform session execution implementation (internal)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from netopsbench.agents.base import DiagnosticContext
from netopsbench.evaluator.fault_type_judge import create_judge_from_env
from netopsbench.evaluator.scorer import Evaluator
from netopsbench.logging_utils import get_logger
from netopsbench.platform.runtime.manager import RuntimeManager, RuntimePool
from netopsbench.platform.scenario.executor import ScenarioExecutor
from netopsbench.platform.session.context import (
    _build_toolkit_for_topology,
    _extract_episode_pingmesh_query_window,
    build_topology_snapshot,
)
from netopsbench.platform.session.diagnosis import AgentHandleAdapter, run_agent_diagnose
from netopsbench.platform.session.dispatch import execute_on_runtime_pool
from netopsbench.platform.session.env import build_worker_execution_context
from netopsbench.platform.session.reporting import (
    LocalArtifactStore,
    artifacts_root,
    build_run_handle,
    create_run_report,
    load_topology_metadata,
    next_run_id,
    resolve_scale,
    save_run_metadata,
    save_run_report,
)
from netopsbench.platform.session.scoring import score_scenario_fault_episodes
from netopsbench.platform.session.types import ScenarioExecutionRef, WorkerExecutionContext
from netopsbench.platform.worker.pool import WorkerSpec
from netopsbench.platform.worker.runtime_agent_input import (
    build_public_case_id,
    build_public_symptoms,
)

logger = get_logger(__name__)


class SessionOrchestrator:
    """Runtime/session orchestration implementation used by SDK managers."""

    def __init__(
        self,
        *,
        platform: Any = None,
        workspace: str = ".",
        runtime_manager: RuntimeManager | None = None,
        artifact_manager: Any | None = None,
        save_report_adapter: Any | None = None,
        run_handle_adapter: Any | None = None,
    ):
        self.platform = platform
        self.name = "sessions"
        self.workspace = Path(workspace)
        self.runtimes = runtime_manager or RuntimeManager(workspace=workspace)
        self.artifacts = artifact_manager or LocalArtifactStore(self.workspace / ".netopsbench" / "artifacts")
        self._save_report_adapter = save_report_adapter or save_run_report
        self._run_handle_adapter = run_handle_adapter or (lambda payload: payload)

    def run_scenario(
        self,
        *,
        scenario: ScenarioExecutionRef | str | Path,
        agent: Any,
        scale: str | None = None,
        workers: int = 1,
        root_dir: str | Path | None = None,
        keep_runtime: bool = False,
        artifacts_dir: str | Path | None = None,
    ) -> Any:
        return self._run_with_platform_runtime(
            mode="scenario",
            scenarios=[self._coerce_scenario(scenario)],
            agent=agent,
            scale=scale,
            workers=workers,
            root_dir=root_dir,
            keep_runtime=keep_runtime,
            artifacts_dir=artifacts_dir,
        )

    def run_suite(
        self,
        *,
        scenarios: Sequence[ScenarioExecutionRef] | str | Path,
        agent: Any,
        scale: str | None = None,
        workers: int = 1,
        root_dir: str | Path | None = None,
        keep_runtime: bool = False,
        artifacts_dir: str | Path | None = None,
    ) -> Any:
        return self._run_with_platform_runtime(
            mode="suite",
            scenarios=self._coerce_scenarios(scenarios),
            agent=agent,
            scale=scale,
            workers=workers,
            root_dir=root_dir,
            keep_runtime=keep_runtime,
            artifacts_dir=artifacts_dir,
        )

    def run_on_runtime_scenario(
        self,
        *,
        scenario: ScenarioExecutionRef | str | Path,
        runtime: RuntimePool,
        agent: Any,
        artifacts_dir: str | Path | None = None,
    ) -> Any:
        return self._run_with_existing_runtime(
            mode="scenario",
            scenarios=[self._coerce_scenario(scenario)],
            runtime=runtime,
            agent=agent,
            artifacts_dir=artifacts_dir,
        )

    def run_on_runtime_suite(
        self,
        *,
        scenarios: Sequence[ScenarioExecutionRef] | str | Path,
        runtime: RuntimePool,
        agent: Any,
        artifacts_dir: str | Path | None = None,
    ) -> Any:
        return self._run_with_existing_runtime(
            mode="suite",
            scenarios=self._coerce_scenarios(scenarios),
            runtime=runtime,
            agent=agent,
            artifacts_dir=artifacts_dir,
        )

    def _run_with_platform_runtime(
        self,
        *,
        mode: str,
        scenarios: list[ScenarioExecutionRef],
        agent: Any,
        scale: str | None,
        workers: int,
        root_dir: str | Path | None,
        keep_runtime: bool,
        artifacts_dir: str | Path | None,
    ) -> Any:
        run_id = self._next_run_id(self._artifacts_root(artifacts_dir))
        runtime_id = f"{run_id}-runtime"
        runtime = self._provision_runtime(
            scale=scale or self._resolve_scale(scenarios),
            workers=workers,
            name=runtime_id,
            root_dir=root_dir,
        )
        try:
            return self._execute_on_runtime_pool(
                run_id=run_id,
                mode=mode,
                scenarios=scenarios,
                runtime=runtime,
                agent=agent,
                artifacts_dir=artifacts_dir,
                runtime_owner="platform",
                teardown=("preserved" if keep_runtime else "performed"),
            )
        finally:
            if not keep_runtime:
                runtime.teardown()

    def _run_with_existing_runtime(
        self,
        *,
        mode: str,
        scenarios: list[ScenarioExecutionRef],
        runtime: RuntimePool,
        agent: Any,
        artifacts_dir: str | Path | None,
    ) -> Any:
        run_id = self._next_run_id(self._artifacts_root(artifacts_dir))
        return self._execute_on_runtime_pool(
            run_id=run_id,
            mode=mode,
            scenarios=scenarios,
            runtime=runtime,
            agent=agent,
            artifacts_dir=artifacts_dir,
            runtime_owner="user",
            teardown="skipped",
        )

    def _execute_on_runtime_pool(
        self,
        *,
        run_id: str,
        mode: str,
        scenarios: list[ScenarioExecutionRef],
        runtime: RuntimePool,
        agent: Any,
        artifacts_dir: str | Path | None,
        runtime_owner: str,
        teardown: str,
    ) -> Any:
        started_at = self._timestamp()
        artifact_dir = self._artifacts_root(artifacts_dir) / run_id
        raw_dir = artifact_dir / "raw"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        raw_dir.mkdir(parents=True, exist_ok=True)
        report_path = artifact_dir / "report.json"
        metadata_path = artifact_dir / "metadata.json"
        return execute_on_runtime_pool(
            run_id=run_id,
            mode=mode,
            scenarios=scenarios,
            runtime=runtime,
            agent=agent,
            artifact_dir=artifact_dir,
            raw_dir=raw_dir,
            report_path=report_path,
            metadata_path=metadata_path,
            runtime_owner=runtime_owner,
            teardown=teardown,
            started_at=started_at,
            completed_at_factory=self._timestamp,
            scenario_runner_cls=ScenarioExecutor,
            evaluator_factory=lambda: (
                Evaluator(fault_type_judge=_j) if (_j := create_judge_from_env()) is not None else Evaluator()
            ),
            score_fault_episodes=score_scenario_fault_episodes,
            diagnosis_callback_builder=self._build_runtime_diagnosis_callback,
            worker_context_builder=self._build_worker_execution_context,
            topology_metadata_loader=self._load_topology_metadata,
            create_run_report=create_run_report,
            save_run_report=self._save_report_adapter,
            save_run_metadata=save_run_metadata,
            build_run_handle=build_run_handle,
            run_handle_adapter=self._run_handle_adapter,
            artifact_manager=self.artifacts,
        )

    def _build_worker_execution_context(self, worker: WorkerSpec, topology_dir: Path) -> WorkerExecutionContext:
        return build_worker_execution_context(worker, topology_dir)

    def _coerce_scenario(self, scenario: Any) -> ScenarioExecutionRef:
        return ScenarioExecutionRef.coerce(scenario)

    def _coerce_scenarios(self, scenarios: Sequence[ScenarioExecutionRef] | str | Path) -> list[ScenarioExecutionRef]:
        if isinstance(scenarios, (str, Path)):
            scenario_path = Path(scenarios)
            if scenario_path.is_dir():
                return [ScenarioExecutionRef.from_path(path) for path in sorted(scenario_path.glob("*.y*ml"))]
            return [ScenarioExecutionRef.from_path(scenario_path)]
        return [self._coerce_scenario(item) for item in scenarios]

    def _provision_runtime(self, *, scale: str, workers: int, name: str, root_dir: str | Path | None) -> RuntimePool:
        runtime_root = (Path(root_dir) / name) if root_dir is not None else None
        return self.runtimes.provision(scale=scale, workers=workers, name=name, root_dir=runtime_root)

    def _build_runtime_diagnosis_callback(
        self,
        agent: Any,
        topology_dir: str,
        scenario_id: str,
        worker_context: WorkerExecutionContext | None = None,
    ):
        toolkit = _build_toolkit_for_topology(topology_dir)
        if worker_context is not None:
            try:
                toolkit.influxdb_bucket = worker_context.influxdb_bucket
                toolkit.topology_id = worker_context.topology_id
            except Exception:
                logger.debug("failed to attach worker context to toolkit", exc_info=True)
        handle = agent if hasattr(agent, "diagnose") and hasattr(agent, "name") else AgentHandleAdapter(agent)

        # Worker env vars (e.g. NETOPSBENCH_TOPOLOGY_DIR) for agent-spawned
        # subprocesses (MCP servers).  Stored in context.metadata["worker_env"]
        # so agents can forward them without mutating process env.
        _worker_env = worker_context.as_env() if worker_context is not None else {}

        def callback(episode_result: dict) -> dict:
            start_time = self._timestamp()
            pingmesh_query_window = _extract_episode_pingmesh_query_window(episode_result)

            context = DiagnosticContext(
                scenario_id=build_public_case_id(scenario_id=scenario_id, episode_result=episode_result),
                topology=build_topology_snapshot(toolkit),
                symptoms=build_public_symptoms(
                    episode_result=episode_result,
                    pingmesh_query_window=pingmesh_query_window,
                ),
                ground_truth=None,
                tools=toolkit,
                metadata={"worker_env": _worker_env} if _worker_env else {},
            )
            diagnosis = self._run_agent_diagnose(handle, context)
            findings = dict(diagnosis.findings or {})
            location = findings.get("location") or {}
            if not isinstance(location, dict):
                location = {}
            return {
                "verdict": diagnosis.verdict,
                "fault_type": findings.get("fault_type") or diagnosis.metadata.get("fault_type"),
                "location": {
                    key: value
                    for key, value in {
                        "device": location.get("device") or findings.get("device"),
                        "interface": location.get("interface") or findings.get("interface"),
                    }.items()
                    if value is not None
                },
                "evidence": list(findings.get("evidence") or []),
                "confidence": float(diagnosis.confidence or 0.0),
                "reasoning": diagnosis.reasoning,
                "tool_calls": list(diagnosis.metadata.get("tool_calls") or []),
                "time_taken_seconds": max(0.0, (self._timestamp() - start_time).total_seconds()),
                "metadata": dict(diagnosis.metadata or {}),
            }

        return callback

    def _run_agent_diagnose(self, handle: Any, context):
        return run_agent_diagnose(handle, context)

    def _load_topology_metadata(self, topology_dir: Path):
        return load_topology_metadata(topology_dir)

    def _artifacts_root(self, artifacts_dir: str | Path | None) -> Path:
        return artifacts_root(self.artifacts, artifacts_dir)

    def _next_run_id(self, artifacts_root_dir: Path) -> str:
        return next_run_id(artifacts_root_dir)

    def _resolve_scale(self, scenarios: Iterable[ScenarioExecutionRef]) -> str:
        return resolve_scale(self.platform, scenarios)

    def _timestamp(self) -> datetime:
        return datetime.now(UTC)


__all__ = ["SessionOrchestrator"]
