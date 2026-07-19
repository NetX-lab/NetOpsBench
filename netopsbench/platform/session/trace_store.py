"""Thread-safe trace persistence and evaluation indexes."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from netopsbench.agents._trace_utils import jsonable as _jsonable
from netopsbench.agents.tracing import AgentTraceRecorder

from .atif import build_atif_payload, build_trace_payload
from .trace_utils import (
    duration_seconds as _duration_seconds,
)
from .trace_utils import (
    load_jsonl as _load_jsonl,
)
from .trace_utils import (
    safe_path_part as _safe_path_part,
)
from .trace_utils import (
    to_json as _to_json,
)


@dataclass(frozen=True)
class TraceWriteResult:
    """Trajectory path written for one diagnosis."""

    trace_id: str
    case_id: str
    worker: str
    atif_path: str


class TraceWriter:
    """Thread-safe writer for per-case ATIF trajectories plus run indexes."""

    def __init__(self, root_dir: str | Path, *, run_id: str):
        self.root_dir = Path(root_dir)
        self.run_id = str(run_id)
        self.index_path = self.root_dir / "index.jsonl"
        self.results_path = self.root_dir / "results.jsonl"
        self._lock = threading.Lock()
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def write_case_trace(
        self,
        *,
        case_id: str,
        scenario_id: str,
        episode_result: dict[str, Any],
        worker: str,
        topology_id: str | None,
        runtime_id: str,
        agent: Any,
        diagnostic_context: Any,
        diagnosis: Any,
        diagnosis_payload: dict[str, Any],
        started_at: datetime,
        ended_at: datetime,
        pingmesh_window: dict[str, Any] | None = None,
        error: str | None = None,
        trace_recorder: AgentTraceRecorder | None = None,
        topology_scale: str | None = None,
    ) -> TraceWriteResult:
        safe_worker = _safe_path_part(worker)
        safe_case_id = _safe_path_part(case_id)
        trace_id = f"{self.run_id}:{safe_worker}:{safe_case_id}:{uuid.uuid4().hex[:12]}"
        case_dir = self.root_dir / safe_worker / safe_case_id
        case_dir.mkdir(parents=True, exist_ok=True)

        trace = build_trace_payload(
            trace_id=trace_id,
            run_id=self.run_id,
            case_id=str(case_id),
            scenario_id=str(scenario_id),
            episode_result=episode_result,
            worker=str(worker),
            topology_id=topology_id,
            runtime_id=str(runtime_id),
            topology_scale=topology_scale,
            agent=agent,
            diagnosis=diagnosis,
            diagnosis_payload=diagnosis_payload,
            started_at=started_at,
            ended_at=ended_at,
            pingmesh_window=pingmesh_window,
            error=error,
            diagnostic_context=diagnostic_context,
            trace_recorder=trace_recorder,
        )
        atif_path = case_dir / "trajectory.atif.json"
        atif_path.write_text(_to_json(build_atif_payload(trace)), encoding="utf-8")

        result = TraceWriteResult(trace_id=trace_id, case_id=str(case_id), worker=str(worker), atif_path=str(atif_path))
        self._append_index(result, trace)
        return result

    def write_evaluation_results(self, *, evaluation_results: list[Any], scenario_result: dict[str, Any]) -> None:
        trace_by_episode = _trace_refs_by_episode(scenario_result)
        trace_by_scenario = _trace_refs_by_scenario(self.index_path)
        rows: list[dict[str, Any]] = []
        for result in evaluation_results:
            payload = result.to_dict() if hasattr(result, "to_dict") else dict(result)
            details = payload.get("details") if isinstance(payload, dict) else {}
            episode_id = (details or {}).get("episode_id") or _episode_id_from_testcase(payload.get("testcase_id"))
            scenario_id = (details or {}).get("scenario_id")
            trace_ref = trace_by_episode.get(str(episode_id)) or trace_by_scenario.get(str(scenario_id)) or {}
            rows.append(
                _jsonable(
                    {
                        "trace_id": trace_ref.get("trace_id"),
                        "atif_path": trace_ref.get("atif_path"),
                        "run_id": self.run_id,
                        "case_id": trace_ref.get("case_id"),
                        "scenario_id": scenario_id,
                        "episode_id": episode_id,
                        "testcase_id": payload.get("testcase_id"),
                        "score": payload.get("score"),
                        "correct_verdict": payload.get("correct_verdict"),
                        "correct_device": payload.get("correct_device"),
                        "correct_interface": payload.get("correct_interface"),
                        "correct_fault_type": payload.get("correct_fault_type"),
                        "details": details or {},
                    }
                )
            )
        if rows:
            self._append_jsonl(self.results_path, rows)

    def write_failure_result(
        self,
        *,
        scenario_id: str,
        scenario_result: dict[str, Any],
        stage: str,
        error: BaseException | str,
    ) -> None:
        trace_by_episode = _trace_refs_by_episode(scenario_result)
        trace_by_scenario = _trace_refs_by_scenario(self.index_path)
        error_type = type(error).__name__ if isinstance(error, BaseException) else "Error"
        rows = [
            {
                "trace_id": trace_ref.get("trace_id"),
                "atif_path": trace_ref.get("atif_path"),
                "run_id": self.run_id,
                "case_id": trace_ref.get("case_id"),
                "scenario_id": scenario_id,
                "episode_id": episode_id,
                "status": "error",
                "error_stage": stage,
                "error_type": error_type,
                "error": str(error),
            }
            for episode_id, trace_ref in trace_by_episode.items()
        ]
        if not rows:
            trace_ref = trace_by_scenario.get(str(scenario_id)) or {}
            rows.append(
                {
                    "trace_id": trace_ref.get("trace_id"),
                    "atif_path": trace_ref.get("atif_path"),
                    "run_id": self.run_id,
                    "case_id": trace_ref.get("case_id"),
                    "scenario_id": scenario_id,
                    "status": "error",
                    "error_stage": stage,
                    "error_type": error_type,
                    "error": str(error),
                }
            )
        self._append_jsonl(self.results_path, [_jsonable(row) for row in rows])

    def _append_index(self, result: TraceWriteResult, trace: dict[str, Any]) -> None:
        metrics = trace.get("metrics") or {}
        agent = trace.get("agent") or {}
        model = trace.get("model") or {}
        started_at = trace.get("started_at")
        ended_at = trace.get("ended_at")
        self._append_jsonl(
            self.index_path,
            [
                {
                    "trace_id": result.trace_id,
                    "run_id": self.run_id,
                    "case_id": result.case_id,
                    "scenario_id": trace.get("scenario_id"),
                    "episode_id": trace.get("episode_id"),
                    "worker": result.worker,
                    "topology_scale": trace.get("topology_scale"),
                    "status": "error" if trace.get("error") else "completed",
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "duration_seconds": _duration_seconds(started_at, ended_at),
                    "agent": agent.get("name"),
                    "agent_class": agent.get("class"),
                    "model": model.get("model"),
                    "provider": model.get("provider"),
                    "runtime": model.get("runtime"),
                    "step_count": len(trace.get("steps") or []),
                    "tool_call_count": metrics.get("tool_calls_count"),
                    "llm_call_count": metrics.get("llm_call_count"),
                    "input_tokens": metrics.get("input_tokens"),
                    "output_tokens": metrics.get("output_tokens"),
                    "total_tokens": metrics.get("total_tokens"),
                    "error_stage": _error_stage(trace),
                    "atif_path": result.atif_path,
                }
            ],
        )

    def _append_jsonl(self, path: Path, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _trace_refs_by_episode(scenario_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    refs: dict[str, dict[str, Any]] = {}
    episode_result = scenario_result.get("episode")
    if isinstance(episode_result, dict):
        episode_id = ((episode_result.get("episode") or {}).get("episode_id")) or "unknown"
        diagnosis = episode_result.get("diagnosis") or {}
        trace = dict(diagnosis.get("trace") or {})
        if trace:
            trace.setdefault("case_id", diagnosis.get("case_id") or diagnosis.get("scenario_id"))
            refs[str(episode_id)] = trace
    return refs


def _trace_refs_by_scenario(index_path: Path) -> dict[str, dict[str, Any]]:
    refs: dict[str, dict[str, Any]] = {}
    for row in _load_jsonl(index_path):
        scenario_id = row.get("scenario_id")
        if scenario_id:
            refs.setdefault(str(scenario_id), row)
    return refs


def _episode_id_from_testcase(testcase_id: Any) -> str | None:
    if not testcase_id:
        return None
    text = str(testcase_id)
    return text.rsplit(":", 1)[1] if ":" in text else None


def _error_stage(trace: dict[str, Any]) -> str | None:
    if not trace.get("error"):
        return None
    metadata = (trace.get("final_diagnosis") or {}).get("metadata") or {}
    return metadata.get("agent_failure_stage") or metadata.get("failure_stage") or "diagnose"


__all__ = ["TraceWriter", "TraceWriteResult"]
