"""Artifact/report helpers for session execution."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from netopsbench.logging_utils import get_logger
from netopsbench.models.scenario import ScenarioSpec
from netopsbench.platform.topology.topology_utils import load_topology_manifest

logger = get_logger(__name__)


class LocalArtifactStore:
    """Minimal platform-local artifact store used when no SDK artifact manager is supplied."""

    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def save_metadata(self, artifact_dir: Path, payload: dict[str, Any]) -> None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = artifact_dir / "metadata.json"
        metadata_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def load_topology_metadata(topology_dir: Path) -> dict[str, Any]:
    return load_topology_manifest(topology_dir).model_dump(mode="json")


def artifacts_root(artifact_manager: Any, artifacts_dir: str | Path | None) -> Path:
    return Path(artifacts_dir) if artifacts_dir is not None else (artifact_manager.root_dir / "runs")


def next_run_id(artifact_root: Path, *, started_at: datetime | None = None) -> str:
    timestamp = (started_at or datetime.now(UTC)).astimezone(UTC)
    base = f"run-{timestamp.strftime('%Y%m%dT%H%M%SZ')}"
    if not artifact_root.exists() or not (artifact_root / base).exists():
        return base
    suffix = 2
    while (artifact_root / f"{base}-{suffix:02d}").exists():
        suffix += 1
    return f"{base}-{suffix:02d}"


def resolve_scale(scenarios: Iterable[ScenarioSpec]) -> str:
    scenario_list = list(scenarios)
    return scenario_list[0].scale if scenario_list else "xs"


def create_run_report(
    *,
    run_id: str,
    mode: str,
    started_at: datetime,
    completed_at: datetime,
    runtime: Any,
    runtime_owner: str,
    teardown: str,
    scenarios: Sequence[ScenarioSpec],
    agent: Any,
    worker_summaries: list[dict[str, Any]],
    scenario_summaries: list[dict[str, Any]],
    aggregate_report: dict[str, Any],
    artifact_dir: Path,
    raw_dir: Path,
    report_path: Path,
    metadata_path: Path,
    traces_dir: Path | None = None,
    trace_index_path: Path | None = None,
    trace_results_path: Path | None = None,
) -> dict[str, Any]:
    overall_success = all(summary.get("success", False) for summary in worker_summaries) if worker_summaries else True
    status = "completed" if overall_success else "failed"
    reported_agent_name = str(
        aggregate_report.get("agent_name") or getattr(agent, "name", agent.__class__.__name__) or "unknown"
    )
    reported_topology_scale = str(
        aggregate_report.get("topology_scale")
        or getattr(runtime, "scale", None)
        or (scenarios[0].scale if scenarios else "unknown")
        or "unknown"
    )
    profile = runtime.scale_registry.get(reported_topology_scale)
    scale_profile = profile.model_dump(mode="json")
    artifact_paths = {
        "report": str(report_path),
        "metadata": str(metadata_path),
        "raw_dir": str(raw_dir),
    }
    if traces_dir is not None:
        artifact_paths["traces_dir"] = str(traces_dir)
    if trace_index_path is not None:
        artifact_paths["trace_index"] = str(trace_index_path)
    if trace_results_path is not None:
        artifact_paths["trace_results"] = str(trace_results_path)

    return {
        "id": f"run:{run_id}",
        "run_id": run_id,
        "agent_name": reported_agent_name,
        "mode": mode,
        "status": status,
        "runtime_id": runtime.id,
        "topology_scale": reported_topology_scale,
        "scale_registry_sha256": runtime.scale_registry.digest,
        "scale_profile_sha256": profile.digest,
        "resolved_scale_profile": scale_profile,
        "summary": {
            **dict(aggregate_report.get("summary") or {}),
            "agent_name": reported_agent_name,
            "mode": mode,
            "status": status,
            "topology_scale": reported_topology_scale,
            "scale_registry_sha256": runtime.scale_registry.digest,
            "scale_profile_sha256": profile.digest,
            "runtime_id": runtime.id,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "total_cases": len(scenarios),
        },
        "scenario_summaries": scenario_summaries,
        "detailed_results": list(aggregate_report.get("detailed_results") or []),
        "artifact_paths": artifact_paths,
        "raw": {
            "status": status,
            "mode": mode,
            "runtime_id": runtime.id,
            "runtime_owner": runtime_owner,
            "teardown": teardown,
            "scenario_ids": [scenario.id for scenario in scenarios],
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "agent": reported_agent_name,
            "topology_scale": reported_topology_scale,
            "scale_registry_sha256": runtime.scale_registry.digest,
            "scale_profile_sha256": profile.digest,
            "resolved_scale_profile": scale_profile,
            "execution": "real_runtime_runner",
            "worker_summaries": worker_summaries,
        },
    }


def save_run_report(report_payload: dict[str, Any], report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report_payload, indent=2, default=str), encoding="utf-8")


def save_run_metadata(
    artifact_manager: Any,
    artifact_dir: Path,
    *,
    run_id: str,
    mode: str,
    status: str,
    runtime_id: str,
    runtime_owner: str,
    teardown: str,
    started_at: datetime,
    completed_at: datetime,
    scenarios: Sequence[ScenarioSpec],
    worker_summaries: list[dict[str, Any]],
    scale_registry_sha256: str,
    scale_profile_sha256: str,
    resolved_scale_profile: dict[str, Any],
    traces_dir: Path | None = None,
    trace_index_path: Path | None = None,
    trace_results_path: Path | None = None,
) -> None:
    artifact_paths = {}
    if traces_dir is not None:
        artifact_paths["traces_dir"] = str(traces_dir)
    if trace_index_path is not None:
        artifact_paths["trace_index"] = str(trace_index_path)
    if trace_results_path is not None:
        artifact_paths["trace_results"] = str(trace_results_path)
    artifact_manager.save_metadata(
        artifact_dir,
        {
            "run_id": run_id,
            "mode": mode,
            "status": status,
            "runtime_id": runtime_id,
            "runtime_owner": runtime_owner,
            "teardown": teardown,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "scenario_ids": [scenario.id for scenario in scenarios],
            "execution": "real_runtime_runner",
            "worker_summaries": worker_summaries,
            "scale_registry_sha256": scale_registry_sha256,
            "scale_profile_sha256": scale_profile_sha256,
            "resolved_scale_profile": resolved_scale_profile,
            "artifact_paths": artifact_paths,
        },
    )


def build_run_handle(
    *,
    run_id: str,
    mode: str,
    status: str,
    started_at: datetime,
    completed_at: datetime,
    artifact_dir: Path,
    scenarios: Sequence[ScenarioSpec],
    runtime_id: str,
    report_path: Path,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "mode": mode,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "artifact_dir": str(artifact_dir),
        "scenario_ids": [scenario.id for scenario in scenarios],
        "runtime_id": runtime_id,
        "report_path": report_path,
    }
