"""Public session manager exports."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from netopsbench.exceptions import ScenarioValidationError
from netopsbench.models.scenario import ScenarioSpec
from netopsbench.platform.session.orchestrator import SessionOrchestrator
from netopsbench.sdk.reports import BenchmarkReport, RunHandle
from netopsbench.sdk.runtimes import RuntimePool


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


class SessionManager:
    """SDK manager delegating benchmark execution to the shared episode kernel."""

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
        scenario: ScenarioSpec | str | Path,
        agent: Any,
        scale: str | None = None,
        workers: int = 1,
        root_dir: str | Path | None = None,
        keep_runtime: bool = False,
        artifacts_dir: str | Path | None = None,
        trace: bool = True,
    ) -> RunHandle:
        return self._executor.run_scenario(
            scenario=scenario,
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
    ) -> RunHandle:
        scenario_list = self._preflight_suite(scenarios)
        return self._executor.run_suite(
            scenarios=scenario_list,
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
    ) -> RunHandle:
        return self._executor.run_on_runtime_scenario(
            scenario=scenario,
            runtime=runtime._runtime,
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
    ) -> RunHandle:
        scenario_list = self._preflight_suite(scenarios)
        if scenario_list[0].scale != runtime.scale:
            raise ScenarioValidationError(
                f"Suite scale {scenario_list[0].scale!r} does not match runtime scale {runtime.scale!r}"
            )
        return self._executor.run_on_runtime_suite(
            scenarios=scenario_list,
            runtime=runtime._runtime,
            agent=agent,
            artifacts_dir=artifacts_dir,
            trace=trace,
        )

    def _preflight_suite(
        self,
        scenarios: Sequence[ScenarioSpec] | str | Path,
    ) -> list[ScenarioSpec]:
        scenario_list = self._executor._coerce_scenarios(scenarios)
        if not scenario_list:
            raise ScenarioValidationError("A benchmark suite must contain at least one scenario")
        scales = sorted({scenario.scale for scenario in scenario_list})
        if len(scales) != 1:
            raise ScenarioValidationError(
                "All scenarios in a suite must use the same topology scale; got: " + ", ".join(scales)
            )
        return scenario_list


__all__ = ["SessionManager"]
