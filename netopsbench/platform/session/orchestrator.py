"""Platform session execution implementation (internal)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from netopsbench.agents.base import DiagnosticContext
from netopsbench.agents.tracing import AgentTraceRecorder
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
from netopsbench.platform.session.tracing import TraceWriter
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
        trace: bool = True,
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
            trace=trace,
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
        trace: bool = True,
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
            trace=trace,
        )

    def run_on_runtime_scenario(
        self,
        *,
        scenario: ScenarioExecutionRef | str | Path,
        runtime: RuntimePool,
        agent: Any,
        artifacts_dir: str | Path | None = None,
        trace: bool = True,
    ) -> Any:
        return self._run_with_existing_runtime(
            mode="scenario",
            scenarios=[self._coerce_scenario(scenario)],
            runtime=runtime,
            agent=agent,
            artifacts_dir=artifacts_dir,
            trace=trace,
        )

    def run_on_runtime_suite(
        self,
        *,
        scenarios: Sequence[ScenarioExecutionRef] | str | Path,
        runtime: RuntimePool,
        agent: Any,
        artifacts_dir: str | Path | None = None,
        trace: bool = True,
    ) -> Any:
        return self._run_with_existing_runtime(
            mode="suite",
            scenarios=self._coerce_scenarios(scenarios),
            runtime=runtime,
            agent=agent,
            artifacts_dir=artifacts_dir,
            trace=trace,
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
        trace: bool,
    ) -> Any:
        started_at = self._timestamp()
        run_id = self._next_run_id(self._artifacts_root(artifacts_dir), started_at=started_at)
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
                trace=trace,
                runtime_owner="platform",
                teardown=("preserved" if keep_runtime else "performed"),
                started_at=started_at,
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
        trace: bool,
    ) -> Any:
        started_at = self._timestamp()
        run_id = self._next_run_id(self._artifacts_root(artifacts_dir), started_at=started_at)
        return self._execute_on_runtime_pool(
            run_id=run_id,
            mode=mode,
            scenarios=scenarios,
            runtime=runtime,
            agent=agent,
            artifacts_dir=artifacts_dir,
            trace=trace,
            runtime_owner="user",
            teardown="skipped",
            started_at=started_at,
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
        trace: bool,
        runtime_owner: str,
        teardown: str,
        started_at: datetime,
    ) -> Any:
        artifact_dir = self._artifacts_root(artifacts_dir) / run_id
        raw_dir = artifact_dir / "raw"
        trace_writer = TraceWriter(artifact_dir / "traces", run_id=run_id) if self._trace_enabled(trace) else None
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
            traces_dir=(trace_writer.root_dir if trace_writer is not None else None),
            trace_writer=trace_writer,
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
        trace_writer: TraceWriter | None = None,
        worker_name: str | None = None,
        run_id: str | None = None,
        runtime_id: str | None = None,
        scenario_scale: str | None = None,
    ):
        toolkit = _build_toolkit_for_topology(topology_dir)
        if worker_context is not None:
            try:
                toolkit.influxdb_bucket = worker_context.influxdb_bucket
                toolkit.topology_id = worker_context.topology_id
            except Exception:
                logger.debug("failed to attach worker context to toolkit", exc_info=True)
        handle = agent if isinstance(agent, AgentHandleAdapter) else AgentHandleAdapter(agent)
        context_dir = Path(topology_dir) / ".netopsbench"
        context_file = context_dir / "pingmesh_context.json"

        # Worker env vars (e.g. NETOPSBENCH_TOPOLOGY_DIR) for agent-spawned
        # subprocesses (MCP servers).  Stored in context.metadata["worker_env"]
        # so agents can forward them without mutating process env.
        _worker_env = worker_context.as_env() if worker_context is not None else {}
        _worker_env["NETOPSBENCH_PINGMESH_CONTEXT_FILE"] = str(context_file)

        def callback(episode_result: dict) -> dict:
            start_time = self._timestamp()
            trace_recorder = AgentTraceRecorder(enabled=trace_writer is not None)
            pingmesh_query_window = _extract_episode_pingmesh_query_window(episode_result)
            window_start = pingmesh_query_window.get("start_time")
            window_end = pingmesh_query_window.get("end_time")
            try:
                toolkit.set_pingmesh_time_window(window_start, window_end)
            except Exception:
                logger.debug("failed to set toolkit pingmesh time window", exc_info=True)
            if window_start and window_end:
                try:
                    context_dir.mkdir(parents=True, exist_ok=True)
                    context_file.write_text(
                        json.dumps({"start_time": window_start, "end_time": window_end}),
                        encoding="utf-8",
                    )
                except Exception:
                    logger.debug("failed to write pingmesh context file", exc_info=True)

            context = DiagnosticContext(
                scenario_id=build_public_case_id(scenario_id=scenario_id, episode_result=episode_result),
                topology=build_topology_snapshot(toolkit),
                symptoms=build_public_symptoms(
                    episode_result=episode_result,
                    pingmesh_query_window=pingmesh_query_window,
                ),
                ground_truth=None,
                tools=toolkit,
                trace=trace_recorder,
                metadata={"worker_env": _worker_env} if _worker_env else {},
            )
            try:
                diagnosis = self._run_agent_diagnose(handle, context)
            except Exception as exc:
                trace_recorder.record_error(stage="agent", error=exc)
                ended_at = self._timestamp()
                diagnosis_payload: dict[str, Any] = {
                    "error": str(exc),
                    "success": False,
                    "time_taken_seconds": max(0.0, (ended_at - start_time).total_seconds()),
                    "metadata": {"agent_failure_stage": "diagnose", "error_type": type(exc).__name__},
                }
                if trace_writer is not None:
                    try:
                        trace_result = trace_writer.write_case_trace(
                            case_id=context.scenario_id,
                            scenario_id=scenario_id,
                            episode_result=episode_result,
                            worker=worker_name or "worker",
                            topology_id=(worker_context.topology_id if worker_context is not None else None),
                            topology_scale=scenario_scale,
                            runtime_id=runtime_id or "",
                            agent=agent,
                            diagnostic_context=context,
                            diagnosis=SimpleNamespace(
                                agent_name=getattr(handle, "name", "agent"),
                                success=False,
                                findings={"error": str(exc)},
                                metadata=diagnosis_payload["metadata"],
                            ),
                            diagnosis_payload=diagnosis_payload,
                            started_at=start_time,
                            ended_at=ended_at,
                            pingmesh_window=pingmesh_query_window,
                            error=str(exc),
                            trace_recorder=trace_recorder,
                        )
                        diagnosis_payload["trace"] = {
                            "trace_id": trace_result.trace_id,
                            "case_id": trace_result.case_id,
                            "worker": trace_result.worker,
                            "atif_path": trace_result.atif_path,
                        }
                    except Exception:
                        logger.debug("failed to persist failed agent runtime trace", exc_info=True)
                diagnosis_payload["metadata"] = _strip_runtime_trace_metadata(diagnosis_payload["metadata"])
                return diagnosis_payload
            findings = dict(diagnosis.findings or {})
            location = findings.get("location") or {}
            if not isinstance(location, dict):
                location = {}
            ended_at = self._timestamp()
            metadata = dict(diagnosis.metadata or {})
            recorder_metrics = trace_recorder.metrics()
            for key in ("input_tokens", "output_tokens", "total_tokens", "llm_call_count"):
                if recorder_metrics.get(key):
                    metadata[key] = recorder_metrics[key]
            recorded_tool_calls = trace_recorder.tool_calls()
            diagnosis_payload = {
                "verdict": diagnosis.verdict,
                "fault_type": findings.get("fault_type") or metadata.get("fault_type"),
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
                "tool_calls": recorded_tool_calls or list(metadata.get("tool_calls") or []),
                "time_taken_seconds": max(0.0, (ended_at - start_time).total_seconds()),
                "metadata": metadata,
            }
            if trace_writer is not None:
                try:
                    trace_result = trace_writer.write_case_trace(
                        case_id=context.scenario_id,
                        scenario_id=scenario_id,
                        episode_result=episode_result,
                        worker=worker_name or "worker",
                        topology_id=(worker_context.topology_id if worker_context is not None else None),
                        topology_scale=scenario_scale,
                        runtime_id=runtime_id or "",
                        agent=agent,
                        diagnostic_context=context,
                        diagnosis=diagnosis,
                        diagnosis_payload=diagnosis_payload,
                        started_at=start_time,
                        ended_at=ended_at,
                        pingmesh_window=pingmesh_query_window,
                        trace_recorder=trace_recorder,
                    )
                    diagnosis_payload["trace"] = {
                        "trace_id": trace_result.trace_id,
                        "case_id": trace_result.case_id,
                        "worker": trace_result.worker,
                        "atif_path": trace_result.atif_path,
                    }
                except Exception:
                    logger.debug("failed to persist agent runtime trace", exc_info=True)
            diagnosis_payload["metadata"] = _strip_runtime_trace_metadata(diagnosis_payload["metadata"])
            return diagnosis_payload

        return callback

    def _run_agent_diagnose(self, handle: Any, context):
        return run_agent_diagnose(handle, context)

    def _load_topology_metadata(self, topology_dir: Path):
        return load_topology_metadata(topology_dir)

    def _artifacts_root(self, artifacts_dir: str | Path | None) -> Path:
        return artifacts_root(self.artifacts, artifacts_dir)

    def _next_run_id(self, artifacts_root_dir: Path, *, started_at: datetime | None = None) -> str:
        return next_run_id(artifacts_root_dir, started_at=started_at)

    def _resolve_scale(self, scenarios: Iterable[ScenarioExecutionRef]) -> str:
        return resolve_scale(self.platform, scenarios)

    def _timestamp(self) -> datetime:
        return datetime.now(UTC)

    def _trace_enabled(self, trace: bool) -> bool:
        if not trace:
            return False
        raw = str(self.env_value("NETOPSBENCH_TRACE", "1")).strip().lower()
        return raw not in {"0", "false", "no", "off"}

    def env_value(self, key: str, default: str = "") -> str:
        env = getattr(self.platform, "env", None)
        if isinstance(env, dict) and key in env:
            return str(env[key])
        import os

        return os.environ.get(key, default)


def _strip_runtime_trace_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    public_metadata = dict(metadata or {})
    public_metadata.pop("trace", None)
    public_metadata.pop("trajectory", None)
    return public_metadata


__all__ = ["SessionOrchestrator"]
