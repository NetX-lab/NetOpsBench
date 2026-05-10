"""Public session manager exports."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from netopsbench.platform.session.orchestrator import SessionOrchestrator
from netopsbench.platform.session.types import ScenarioExecutionRef
from netopsbench.sdk.reports import BenchmarkReport, RunHandle
from netopsbench.sdk.runtimes import RuntimePool
from netopsbench.sdk.scenarios import ScenarioHandle


def _benchmark_report_from_payload(payload: dict[str, Any]) -> BenchmarkReport:
    return BenchmarkReport(
        id=str(payload.get("id") or f"run:{payload.get('run_id', '')}"),
        summary=dict(payload.get("summary") or {}),
        scenario_summaries=list(payload.get("scenario_summaries") or []),
        detailed_results=list(payload.get("detailed_results") or []),
        artifact_paths=dict(payload.get("artifact_paths") or {}),
        raw=dict(payload.get("raw") or {}),
    )


def _save_report_payload_as_sdk_report(payload: dict[str, Any], report_path: Path) -> None:
    _benchmark_report_from_payload(payload).save(report_path)


def _run_handle_from_payload(payload: dict[str, Any]) -> RunHandle:
    return RunHandle(
        id=str(payload["id"]),
        mode=str(payload["mode"]),
        status=str(payload["status"]),
        started_at=payload["started_at"],
        completed_at=payload["completed_at"],
        artifact_dir=str(payload["artifact_dir"]),
        scenario_ids=list(payload.get("scenario_ids") or []),
        runtime_id=str(payload["runtime_id"]),
        report_path=Path(payload["report_path"]),
    )


def _coerce_public_scenario_input(scenario: ScenarioHandle | ScenarioExecutionRef | str | Path):
    if isinstance(scenario, ScenarioHandle):
        return ScenarioExecutionRef.from_scenario(scenario.to_scenario(), path=scenario.path)
    return scenario


def _coerce_public_scenario_inputs(scenarios: Sequence[ScenarioHandle | ScenarioExecutionRef] | str | Path):
    if isinstance(scenarios, (str, Path)):
        return scenarios
    return [_coerce_public_scenario_input(item) for item in scenarios]


class SessionManager:
    """Thin SDK manager delegating runtime execution to platform internals."""

    def __init__(
        self,
        *,
        platform: Any = None,
        workspace: str = ".",
        runtime_manager: Any | None = None,
        artifact_manager: Any | None = None,
    ):
        self.platform = platform
        self.name = "sessions"
        self._executor = SessionOrchestrator(
            platform=platform,
            workspace=workspace,
            runtime_manager=runtime_manager,
            artifact_manager=artifact_manager,
            save_report_adapter=_save_report_payload_as_sdk_report,
            run_handle_adapter=_run_handle_from_payload,
        )

    def run_scenario(
        self,
        *,
        scenario: ScenarioHandle | str | Path,
        agent: Any,
        scale: str | None = None,
        workers: int = 1,
        root_dir: str | Path | None = None,
        keep_runtime: bool = False,
        artifacts_dir: str | Path | None = None,
    ) -> RunHandle:
        return self._executor.run_scenario(
            scenario=_coerce_public_scenario_input(scenario),
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
        scenarios: Sequence[ScenarioHandle] | str | Path,
        agent: Any,
        scale: str | None = None,
        workers: int = 1,
        root_dir: str | Path | None = None,
        keep_runtime: bool = False,
        artifacts_dir: str | Path | None = None,
    ) -> RunHandle:
        return self._executor.run_suite(
            scenarios=_coerce_public_scenario_inputs(scenarios),
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
        scenario: ScenarioHandle | str | Path,
        runtime: RuntimePool,
        agent: Any,
        artifacts_dir: str | Path | None = None,
    ) -> RunHandle:
        return self._executor.run_on_runtime_scenario(
            scenario=_coerce_public_scenario_input(scenario),
            runtime=runtime,
            agent=agent,
            artifacts_dir=artifacts_dir,
        )

    def run_on_runtime_suite(
        self,
        *,
        scenarios: Sequence[ScenarioHandle] | str | Path,
        runtime: RuntimePool,
        agent: Any,
        artifacts_dir: str | Path | None = None,
    ) -> RunHandle:
        return self._executor.run_on_runtime_suite(
            scenarios=_coerce_public_scenario_inputs(scenarios),
            runtime=runtime,
            agent=agent,
            artifacts_dir=artifacts_dir,
        )


__all__ = ["SessionManager"]
