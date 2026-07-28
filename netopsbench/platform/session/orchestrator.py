"""Platform session execution implementation (internal)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from netopsbench.evaluator.scorer import Evaluator
from netopsbench.logging_utils import get_logger
from netopsbench.models.scenario import ScenarioSpec
from netopsbench.platform.runtime.manager import RuntimeManager, RuntimePool
from netopsbench.platform.scenario.parser import parse_scenario_file
from netopsbench.platform.session.dispatch import execute_on_runtime_pool
from netopsbench.platform.session.reporting import (
    LocalArtifactStore,
    artifacts_root,
    build_run_handle,
    create_run_report,
    reserve_run_id,
    resolve_scale,
    save_run_metadata,
    save_run_report,
)
from netopsbench.platform.session.trace_store import TraceWriter
from netopsbench.platform.utils.files import atomic_write_json

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
        scenario: ScenarioSpec | str | Path,
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
        scenarios: Sequence[ScenarioSpec] | str | Path,
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
        scenario: ScenarioSpec | str | Path,
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
        scenarios: Sequence[ScenarioSpec] | str | Path,
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
        scenarios: list[ScenarioSpec],
        agent: Any,
        scale: str | None,
        workers: int,
        root_dir: str | Path | None,
        keep_runtime: bool,
        artifacts_dir: str | Path | None,
        trace: bool,
    ) -> Any:
        started_at = self._timestamp()
        run_id = reserve_run_id(self._artifacts_root(artifacts_dir), started_at=started_at)
        runtime_id = f"{run_id}-runtime"
        runtime = self._provision_runtime(
            scale=scale or self._resolve_scale(scenarios),
            workers=workers,
            name=runtime_id,
            root_dir=root_dir,
        )
        execution_failure: Exception | None = None
        cleanup_failure: Exception | None = None
        handle: Any | None = None
        try:
            handle = self._execute_on_runtime_pool(
                run_id=run_id,
                mode=mode,
                scenarios=scenarios,
                runtime=runtime,
                agent=agent,
                artifacts_dir=artifacts_dir,
                trace=trace,
                runtime_owner="platform",
                teardown=("preserved" if keep_runtime else "pending"),
                started_at=started_at,
            )
        except Exception as exc:
            execution_failure = exc
        if not keep_runtime:
            try:
                runtime.teardown()
            except Exception as exc:
                cleanup_failure = exc
        teardown = "preserved" if keep_runtime else ("failed" if cleanup_failure is not None else "performed")
        self._finalize_lifecycle_report(
            run_id=run_id,
            mode=mode,
            scenarios=scenarios,
            runtime=runtime,
            agent=agent,
            artifacts_dir=artifacts_dir,
            started_at=started_at,
            runtime_owner="platform",
            teardown=teardown,
            execution_failure=execution_failure,
            cleanup_failure=cleanup_failure,
        )
        if execution_failure is not None:
            if cleanup_failure is not None:
                execution_failure.add_note(
                    f"Runtime cleanup also failed: {type(cleanup_failure).__name__}: {cleanup_failure}"
                )
            raise execution_failure
        if cleanup_failure is not None:
            raise cleanup_failure
        if handle is not None and hasattr(handle, "refresh"):
            handle.refresh()
        return handle

    def _run_with_existing_runtime(
        self,
        *,
        mode: str,
        scenarios: list[ScenarioSpec],
        runtime: RuntimePool,
        agent: Any,
        artifacts_dir: str | Path | None,
        trace: bool,
    ) -> Any:
        started_at = self._timestamp()
        run_id = reserve_run_id(self._artifacts_root(artifacts_dir), started_at=started_at)
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
        scenarios: list[ScenarioSpec],
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
        trace_writer = TraceWriter(artifact_dir / "traces", run_id=run_id) if trace else None
        artifact_dir.mkdir(parents=True, exist_ok=True)
        raw_dir.mkdir(parents=True, exist_ok=True)
        report_path = artifact_dir / "report.json"
        metadata_path = artifact_dir / "metadata.json"
        dispatched = execute_on_runtime_pool(
            scenarios=scenarios,
            runtime=runtime,
            agent=agent,
            raw_dir=raw_dir,
            trace_writer=trace_writer,
            fault_registry=getattr(getattr(self.platform, "faults", None), "spec_registry", None),
        )
        aggregate_report = (
            Evaluator().generate_report(
                dispatched.evaluations,
                agent_name=getattr(agent, "name", agent.__class__.__name__),
                topology_scale=runtime.scale,
            )
            if dispatched.evaluations
            else {"summary": {"total_cases": 0, "average_score": 0.0}, "detailed_results": []}
        )
        completed_at = self._timestamp()
        traces_dir = trace_writer.root_dir if trace_writer is not None else None
        report_payload = create_run_report(
            run_id=run_id,
            mode=mode,
            started_at=started_at,
            completed_at=completed_at,
            runtime=runtime,
            runtime_owner=runtime_owner,
            teardown=teardown,
            scenarios=scenarios,
            agent=agent,
            worker_summaries=dispatched.workers,
            scenario_summaries=dispatched.scenarios,
            aggregate_report=aggregate_report,
            artifact_dir=artifact_dir,
            raw_dir=raw_dir,
            traces_dir=traces_dir,
            trace_index_path=(trace_writer.index_path if trace_writer is not None else None),
            trace_results_path=(trace_writer.results_path if trace_writer is not None else None),
            report_path=report_path,
            metadata_path=metadata_path,
        )
        self._save_report_adapter(report_payload, report_path)
        report_status = str(
            (report_payload.get("raw") or {}).get("status")
            or (report_payload.get("summary") or {}).get("status")
            or report_payload.get("status")
            or "unknown"
        )
        save_run_metadata(
            self.artifacts,
            artifact_dir,
            run_id=run_id,
            mode=mode,
            status=report_status,
            runtime_id=runtime.id,
            runtime_owner=runtime_owner,
            teardown=teardown,
            started_at=started_at,
            completed_at=completed_at,
            scenarios=scenarios,
            worker_summaries=dispatched.workers,
            scale_registry_sha256=runtime.scale_registry.digest,
            scale_profile_sha256=runtime.scale_registry.get(runtime.scale).digest,
            resolved_scale_profile=runtime.scale_registry.get(runtime.scale).model_dump(mode="json"),
            traces_dir=traces_dir,
            trace_index_path=(trace_writer.index_path if trace_writer is not None else None),
            trace_results_path=(trace_writer.results_path if trace_writer is not None else None),
        )
        handle_payload = build_run_handle(
            run_id=run_id,
            mode=mode,
            status=report_status,
            started_at=started_at,
            completed_at=completed_at,
            artifact_dir=artifact_dir,
            scenarios=scenarios,
            runtime_id=runtime.id,
            report_path=report_path,
        )
        return self._run_handle_adapter(handle_payload)

    def _finalize_lifecycle_report(
        self,
        *,
        run_id: str,
        mode: str,
        scenarios: list[ScenarioSpec],
        runtime: RuntimePool,
        agent: Any,
        artifacts_dir: str | Path | None,
        started_at: datetime,
        runtime_owner: str,
        teardown: str,
        execution_failure: Exception | None,
        cleanup_failure: Exception | None,
    ) -> None:
        artifact_dir = self._artifacts_root(artifacts_dir) / run_id
        report_path = artifact_dir / "report.json"
        metadata_path = artifact_dir / "metadata.json"
        completed_at = self._timestamp()

        def failure_payload(error: Exception | None) -> dict[str, str] | None:
            if error is None:
                return None
            return {"type": type(error).__name__, "message": str(error)}

        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            profile = runtime.scale_registry.get(runtime.scale)
            report = {
                "id": f"run:{run_id}",
                "run_id": run_id,
                "agent_name": getattr(agent, "name", agent.__class__.__name__),
                "mode": mode,
                "status": "failed",
                "runtime_id": runtime.id,
                "topology_scale": runtime.scale,
                "scale_registry_sha256": runtime.scale_registry.digest,
                "scale_profile_sha256": profile.digest,
                "resolved_scale_profile": profile.model_dump(mode="json"),
                "summary": {
                    "status": "failed",
                    "mode": mode,
                    "runtime_id": runtime.id,
                    "started_at": started_at.isoformat(),
                    "completed_at": completed_at.isoformat(),
                    "total_cases": len(scenarios),
                },
                "scenario_summaries": [],
                "detailed_results": [],
                "artifact_paths": {
                    "report": str(report_path),
                    "metadata": str(metadata_path),
                    "raw_dir": str(artifact_dir / "raw"),
                },
                "raw": {
                    "status": "failed",
                    "mode": mode,
                    "runtime_id": runtime.id,
                    "runtime_owner": runtime_owner,
                    "scenario_ids": [scenario.id for scenario in scenarios],
                    "started_at": started_at.isoformat(),
                    "completed_at": completed_at.isoformat(),
                },
            }
        raw = report.setdefault("raw", {})
        raw["teardown"] = teardown
        raw["execution_failure"] = failure_payload(execution_failure)
        raw["cleanup_failure"] = failure_payload(cleanup_failure)
        raw["completed_at"] = completed_at.isoformat()
        failed = execution_failure is not None or cleanup_failure is not None
        if failed:
            report["status"] = "failed"
            raw["status"] = "failed"
            report.setdefault("summary", {})["status"] = "failed"
        report.setdefault("summary", {})["completed_at"] = completed_at.isoformat()
        atomic_write_json(report_path, report, default=str)

        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            metadata = {
                "run_id": run_id,
                "mode": mode,
                "runtime_id": runtime.id,
                "runtime_owner": runtime_owner,
                "scenario_ids": [scenario.id for scenario in scenarios],
                "started_at": started_at.isoformat(),
            }
        metadata.update(
            {
                "status": "failed" if failed else str(raw.get("status") or "completed"),
                "teardown": teardown,
                "execution_failure": failure_payload(execution_failure),
                "cleanup_failure": failure_payload(cleanup_failure),
                "completed_at": completed_at.isoformat(),
            }
        )
        atomic_write_json(metadata_path, metadata, default=str)

    def _coerce_scenario(self, scenario: Any) -> ScenarioSpec:
        if isinstance(scenario, ScenarioSpec):
            return scenario
        if isinstance(scenario, (str, Path)):
            return parse_scenario_file(scenario)
        raise TypeError(f"Unsupported scenario value: {type(scenario)!r}")

    def _coerce_scenarios(self, scenarios: Sequence[ScenarioSpec] | str | Path) -> list[ScenarioSpec]:
        if isinstance(scenarios, (str, Path)):
            scenario_path = Path(scenarios)
            if scenario_path.is_dir():
                return [parse_scenario_file(path) for path in sorted(scenario_path.glob("*.y*ml"))]
            return [parse_scenario_file(scenario_path)]
        return [self._coerce_scenario(item) for item in scenarios]

    def _provision_runtime(self, *, scale: str, workers: int, name: str, root_dir: str | Path | None) -> RuntimePool:
        runtime_root = (Path(root_dir) / name) if root_dir is not None else None
        return self.runtimes.provision(scale=scale, workers=workers, name=name, root_dir=runtime_root)

    def _artifacts_root(self, artifacts_dir: str | Path | None) -> Path:
        return artifacts_root(self.artifacts, artifacts_dir)

    def _resolve_scale(self, scenarios: Iterable[ScenarioSpec]) -> str:
        return resolve_scale(scenarios)

    def _timestamp(self) -> datetime:
        return datetime.now(UTC)


__all__ = ["SessionOrchestrator"]
