"""Artifact/report helpers for session execution."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from netopsbench.logging_utils import get_logger
from netopsbench.platform.session.types import ScenarioExecutionRef

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


def load_topology_metadata(topology_dir: Path) -> dict[str, Any] | None:
    topology_json = topology_dir / "topology.json"
    if not topology_json.exists():
        return None
    try:
        return json.loads(topology_json.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("failed to parse topology.json at %s", topology_json, exc_info=True)
        return None


def artifacts_root(artifact_manager: Any, artifacts_dir: str | Path | None) -> Path:
    return Path(artifacts_dir) if artifacts_dir is not None else (artifact_manager.root_dir / "runs")


def next_run_id(artifact_root: Path) -> str:
    if not artifact_root.exists():
        return "run-0001"
    indices: list[int] = []
    for child in artifact_root.iterdir():
        if not child.is_dir() or not child.name.startswith("run-"):
            continue
        suffix = child.name[4:]
        if suffix.isdigit():
            indices.append(int(suffix))
    next_index = (max(indices) + 1) if indices else 1
    return f"run-{next_index:04d}"


def resolve_scale(platform: Any, scenarios: Iterable[ScenarioExecutionRef]) -> str:
    scenario_list = list(scenarios)
    if scenario_list:
        return scenario_list[0].scale
    defaults = getattr(platform, "defaults", {}) or {}
    return str(defaults.get("scale") or "xs")


def create_run_report(
    *,
    run_id: str,
    mode: str,
    started_at: datetime,
    completed_at: datetime,
    runtime: Any,
    runtime_owner: str,
    teardown: str,
    scenarios: Sequence[ScenarioExecutionRef],
    agent: Any,
    worker_summaries: list[dict[str, Any]],
    scenario_summaries: list[dict[str, Any]],
    aggregate_report: dict[str, Any],
    artifact_dir: Path,
    raw_dir: Path,
    report_path: Path,
    metadata_path: Path,
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
    return {
        "id": f"run:{run_id}",
        "run_id": run_id,
        "agent_name": reported_agent_name,
        "mode": mode,
        "status": status,
        "runtime_id": runtime.id,
        "topology_scale": reported_topology_scale,
        "summary": {
            **dict(aggregate_report.get("summary") or {}),
            "agent_name": reported_agent_name,
            "mode": mode,
            "status": status,
            "topology_scale": reported_topology_scale,
            "runtime_id": runtime.id,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "total_cases": len(scenarios),
        },
        "scenario_summaries": scenario_summaries,
        "detailed_results": list(aggregate_report.get("detailed_results") or []),
        "artifact_paths": {
            "report": str(report_path),
            "metadata": str(metadata_path),
            "raw_dir": str(raw_dir),
        },
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
    scenarios: Sequence[ScenarioExecutionRef],
    worker_summaries: list[dict[str, Any]],
) -> None:
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
    scenarios: Sequence[ScenarioExecutionRef],
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
