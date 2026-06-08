"""Harbor-compatible trajectory persistence for session execution."""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harbor.models.agent.context import AgentContext
from harbor.models.job.config import DatasetConfig, JobConfig
from harbor.models.job.result import JobResult, JobStats
from harbor.models.task.id import LocalTaskId
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig, VerifierConfig
from harbor.models.trial.result import AgentInfo, ModelInfo, TimingInfo, TrialResult
from harbor.models.verifier.result import VerifierResult

from netopsbench.agents._trace_utils import jsonable as _jsonable
from netopsbench.agents.tracing import AgentTraceRecorder

ATIF_SCHEMA_VERSION = "ATIF-v1.7"


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


def build_trace_payload(
    *,
    trace_id: str,
    run_id: str,
    case_id: str,
    scenario_id: str,
    episode_result: dict[str, Any],
    worker: str,
    topology_id: str | None,
    runtime_id: str,
    agent: Any,
    diagnosis: Any,
    diagnosis_payload: dict[str, Any],
    started_at: datetime,
    ended_at: datetime,
    pingmesh_window: dict[str, Any] | None = None,
    error: str | None = None,
    diagnostic_context: Any = None,
    trace_recorder: AgentTraceRecorder | None = None,
    topology_scale: str | None = None,
) -> dict[str, Any]:
    metadata = dict(getattr(diagnosis, "metadata", None) or {})
    model = _agent_model_payload(agent)
    model = {**model, **_model_payload(metadata)}
    if trace_recorder is not None:
        model = {**model, **trace_recorder.model_metadata()}
    steps = _context_steps(diagnostic_context)
    runtime_steps = trace_recorder.to_steps() if trace_recorder is not None else []
    steps.extend(_normalise_steps(runtime_steps, model_metadata=model))
    if not runtime_steps:
        steps.extend(_steps_from_tool_calls(diagnosis_payload.get("tool_calls") or metadata.get("tool_calls") or []))
    steps.append(_final_diagnosis_step(diagnosis_payload, ended_at=ended_at))
    return {
        "trace_id": trace_id,
        "run_id": run_id,
        "case_id": case_id,
        "scenario_id": scenario_id,
        "episode_id": (episode_result.get("episode") or {}).get("episode_id"),
        "worker": worker,
        "topology_id": topology_id,
        "topology_scale": topology_scale,
        "runtime_id": runtime_id,
        "agent": _agent_payload(agent, diagnosis),
        "model": model,
        "pingmesh_window": _jsonable(pingmesh_window or {}),
        "started_at": _isoformat(started_at),
        "ended_at": _isoformat(ended_at),
        "steps": _jsonable(steps),
        "final_diagnosis": _diagnosis_payload(diagnosis_payload),
        "metrics": _metrics_payload(
            metadata,
            diagnosis_payload,
            trace_recorder=trace_recorder,
            started_at=started_at,
            ended_at=ended_at,
        ),
        "error": error or _diagnosis_error(diagnosis, diagnosis_payload),
    }


def build_atif_payload(trace: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": ATIF_SCHEMA_VERSION,
        "session_id": trace.get("run_id"),
        "trajectory_id": trace.get("trace_id"),
        "agent": _atif_agent(trace),
        "steps": _atif_steps(trace),
        "notes": None,
        "final_metrics": _atif_final_metrics(trace),
        "extra": {
            "framework": "netopsbench",
            "case_id": trace.get("case_id"),
            "scenario_id": trace.get("scenario_id"),
            "episode_id": trace.get("episode_id"),
            "runtime_id": trace.get("runtime_id"),
            "worker": trace.get("worker"),
            "pingmesh_window": trace.get("pingmesh_window") or {},
            "topology_id": trace.get("topology_id"),
            "topology_scale": trace.get("topology_scale"),
            "started_at": trace.get("started_at"),
            "ended_at": trace.get("ended_at"),
            "final_diagnosis": trace.get("final_diagnosis") or {},
            "error": trace.get("error"),
        },
    }


def export_traces(run_dir: str | Path, *, output: str | Path) -> Path:
    """Export a NetOpsBench run into a local Harbor viewer jobs directory."""
    run_path = Path(run_dir)
    traces_dir = run_path / "traces"
    if not traces_dir.is_dir():
        raise FileNotFoundError(f"trace directory not found: {traces_dir}")

    index_rows = load_trace_index(run_path)
    if not index_rows:
        raise FileNotFoundError(f"trace index is empty or missing: {traces_dir / 'index.jsonl'}")

    output_root = Path(output)
    output_root.mkdir(parents=True, exist_ok=True)
    job_name = f"netopsbench-{run_path.name}"
    job_dir = output_root / job_name
    if job_dir.exists():
        shutil.rmtree(job_dir)
    job_dir.mkdir(parents=True)

    job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"netopsbench:{run_path.name}"))
    result_rows = load_trace_results(run_path)
    trial_results = [
        _write_harbor_trial(
            job_dir,
            job_id=job_id,
            index_row=row,
            atif=json.loads(_resolve_atif_path(run_path, row).read_text(encoding="utf-8")),
            result_row=_matching_result_row(result_rows, row),
        )
        for row in index_rows
    ]
    job_config = _harbor_job_config(job_name, output_root, index_rows)
    (job_dir / "config.json").write_text(_to_json(job_config.model_dump(mode="json")), encoding="utf-8")
    (job_dir / "result.json").write_text(
        _to_json(_harbor_job_result(job_id, _run_times(run_path, index_rows), trial_results).model_dump(mode="json")),
        encoding="utf-8",
    )
    return output_root


def load_trace_index(run_dir: str | Path) -> list[dict[str, Any]]:
    return _load_jsonl(Path(run_dir) / "traces" / "index.jsonl")


def load_trace_results(run_dir: str | Path) -> list[dict[str, Any]]:
    return _load_jsonl(Path(run_dir) / "traces" / "results.jsonl")


def _write_harbor_trial(
    job_dir: Path,
    *,
    job_id: str,
    index_row: dict[str, Any],
    atif: dict[str, Any],
    result_row: dict[str, Any] | None,
) -> TrialResult:
    case_id = str(index_row.get("case_id") or (atif.get("extra") or {}).get("case_id") or "case")
    scenario_id = str(index_row.get("scenario_id") or (atif.get("extra") or {}).get("scenario_id") or "scenario")
    trial_name = _safe_path_part(f"{scenario_id}__{case_id}")
    trial_dir = job_dir / trial_name
    agent_dir = trial_dir / "agent"
    verifier_dir = trial_dir / "verifier"
    task_dir = trial_dir / "task"
    agent_dir.mkdir(parents=True)
    verifier_dir.mkdir(parents=True)
    task_dir.mkdir(parents=True)

    score = float((result_row or {}).get("score") or 0.0)
    (agent_dir / "trajectory.json").write_text(_to_json(atif), encoding="utf-8")
    (verifier_dir / "reward.txt").write_text(str(score), encoding="utf-8")
    (verifier_dir / "result.json").write_text(
        _to_json({"reward": score, "netopsbench_result": result_row or {}}),
        encoding="utf-8",
    )
    (verifier_dir / "test-stdout.txt").write_text(_to_json(result_row or {}), encoding="utf-8")
    (verifier_dir / "test-stderr.txt").write_text("", encoding="utf-8")
    (task_dir / "instruction.md").write_text(
        f"NetOpsBench scenario {scenario_id} episode {index_row.get('episode_id') or ''}\n",
        encoding="utf-8",
    )

    config = _harbor_trial_config(job_id, job_dir, task_dir, trial_name, index_row)
    trial_result = _harbor_trial_result(trial_dir, task_dir, trial_name, scenario_id, case_id, config, index_row, score)
    (trial_dir / "config.json").write_text(_to_json(config.model_dump(mode="json")), encoding="utf-8")
    (trial_dir / "result.json").write_text(_to_json(trial_result.model_dump(mode="json")), encoding="utf-8")
    return trial_result


def _harbor_trial_config(
    job_id: str,
    job_dir: Path,
    task_dir: Path,
    trial_name: str,
    index_row: dict[str, Any],
) -> TrialConfig:
    return TrialConfig(
        task=TaskConfig(path=task_dir, source=_dataset_source(index_row)),
        trial_name=trial_name,
        trials_dir=job_dir,
        agent=AgentConfig(name=index_row.get("agent") or "unknown", model_name=index_row.get("model")),
        environment=EnvironmentConfig(type="docker"),
        verifier=VerifierConfig(disable=False),
        artifacts=[],
        extra_instruction_paths=[],
        job_id=job_id,
    )


def _harbor_trial_result(
    trial_dir: Path,
    task_dir: Path,
    trial_name: str,
    scenario_id: str,
    case_id: str,
    config: TrialConfig,
    index_row: dict[str, Any],
    score: float,
) -> TrialResult:
    started_at = index_row.get("started_at")
    ended_at = index_row.get("ended_at") or started_at
    return TrialResult(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"{index_row.get('trace_id')}:{trial_name}"),
        task_name=scenario_id,
        trial_name=trial_name,
        trial_uri=str(trial_dir),
        task_id=LocalTaskId(path=task_dir),
        source=_dataset_source(index_row),
        task_checksum=case_id,
        config=config,
        agent_info=AgentInfo(
            name=index_row.get("agent") or "unknown",
            version=index_row.get("agent_class") or "unknown",
            model_info=ModelInfo(name=index_row.get("model") or "unknown", provider=index_row.get("provider")),
        ),
        agent_result=AgentContext(
            n_input_tokens=index_row.get("input_tokens"),
            n_output_tokens=index_row.get("output_tokens"),
            metadata={
                "trace_id": index_row.get("trace_id"),
                "step_count": index_row.get("step_count"),
                "topology_scale": index_row.get("topology_scale"),
                "provider": index_row.get("provider"),
                "model": index_row.get("model"),
            },
        ),
        verifier_result=VerifierResult(rewards={"reward": score, "score": score}),
        started_at=started_at,
        finished_at=ended_at,
        agent_execution=TimingInfo(started_at=started_at, finished_at=ended_at),
        verifier=TimingInfo(started_at=ended_at, finished_at=ended_at),
    )


def _harbor_job_config(job_name: str, output_root: Path, index_rows: list[dict[str, Any]]) -> JobConfig:
    agent_configs: dict[tuple[str, str | None], AgentConfig] = {}
    dataset_names = sorted({_dataset_source(row) for row in index_rows})
    for row in index_rows:
        agent_name = str(row.get("agent") or "unknown")
        model_name = _job_config_model_name(row)
        agent_configs.setdefault((agent_name, model_name), AgentConfig(name=agent_name, model_name=model_name))
    return JobConfig(
        job_name=job_name,
        jobs_dir=output_root,
        agents=list(agent_configs.values()),
        datasets=[DatasetConfig(name=name) for name in dataset_names],
        environment=EnvironmentConfig(type="docker"),
    )


def _harbor_job_result(job_id: str, run_times: dict[str, str], trial_results: list[TrialResult]) -> JobResult:
    stats = JobStats.from_trial_results(trial_results, n_total_trials=len(trial_results))
    _attach_mean_reward_metrics(stats, trial_results)
    return JobResult(
        id=job_id,
        started_at=run_times["started_at"],
        updated_at=run_times["finished_at"],
        finished_at=run_times["finished_at"],
        n_total_trials=len(trial_results),
        stats=stats,
        trial_results=trial_results,
    )


def _normalise_steps(steps: list[Any], *, model_metadata: dict[str, Any]) -> list[dict[str, Any]]:
    normalised: list[dict[str, Any]] = []
    saw_runtime_event = False
    seen_copied_context: set[str] = set()
    for index, item in enumerate(steps, 1):
        step = dict(item) if isinstance(item, dict) else {"content": str(item)}
        step.setdefault("index", index)
        step["type"] = str(step.get("type") or step.get("role") or step.get("source") or "message")
        if step.get("is_copied_context"):
            fingerprint = _message_fingerprint(step)
            if saw_runtime_event or fingerprint in seen_copied_context:
                continue
            seen_copied_context.add(fingerprint)
            step.pop("is_copied_context", None)
            step["is_initial_context"] = True
        else:
            saw_runtime_event = True
        if model_metadata and step["type"] == "llm":
            step.setdefault("model", model_metadata.get("model"))
            step.setdefault("provider", model_metadata.get("provider"))
        normalised.append(_jsonable(step))
    return normalised


def _atif_steps(trace: dict[str, Any]) -> list[dict[str, Any]]:
    atif_steps: list[dict[str, Any]] = []
    for step_id, native in enumerate(_collapse_copied_context_steps(trace.get("steps") or []), 1):
        step = native if isinstance(native, dict) else {"content": str(native)}
        atif_step: dict[str, Any] = {
            "step_id": step_id,
            "timestamp": step.get("started_at") or step.get("ended_at") or trace.get("started_at"),
            "source": _atif_source(step),
            "message": _atif_message_content(_step_message(step) or ""),
            "extra": _compact_extra(step),
        }
        if atif_step["source"] == "agent" and (step.get("model") or (trace.get("model") or {}).get("model")):
            atif_step["model_name"] = step.get("model") or (trace.get("model") or {}).get("model")
        if step.get("reasoning_content"):
            atif_step["reasoning_content"] = step.get("reasoning_content")
        metrics = _atif_step_metrics(step)
        if metrics:
            atif_step["metrics"] = metrics
        if _is_tool_step(step):
            tool_call_id = str(step.get("tool_call_id") or step.get("run_id") or f"tool-{step_id}")
            function_name = step.get("name") or step.get("tool_name") or step.get("tool") or "tool"
            atif_step["source"] = "agent"
            atif_step["message"] = atif_step["message"] or f"Tool call: {function_name}"
            atif_step["tool_calls"] = [
                {
                    "tool_call_id": tool_call_id,
                    "function_name": function_name,
                    "arguments": step.get("args") or step.get("input") or {},
                    "extra": {"run_id": step.get("run_id"), "parent_run_id": step.get("parent_run_id")},
                }
            ]
            observation = step.get("observation")
            if observation is None and step.get("error") is not None:
                observation = {"error": step.get("error")}
            if observation is not None:
                atif_step["observation"] = {
                    "results": [
                        {
                            "source_call_id": tool_call_id,
                            "content": _atif_observation_content(observation),
                            "extra": {"status": "error" if step.get("error") else "success"},
                        }
                    ]
                }
        atif_steps.append(_jsonable(atif_step))
    return atif_steps


def _atif_agent(trace: dict[str, Any]) -> dict[str, Any]:
    agent = trace.get("agent") or {}
    model = trace.get("model") or {}
    return _jsonable(
        {
            "name": agent.get("name") or "unknown",
            "version": agent.get("class") or "unknown",
            "model_name": model.get("model") or "unknown",
            "extra": {"class": agent.get("class"), "provider": model.get("provider"), "runtime": model.get("runtime")},
        }
    )


def _atif_final_metrics(trace: dict[str, Any]) -> dict[str, Any]:
    metrics = trace.get("metrics") or {}
    return _jsonable(
        {
            "total_prompt_tokens": metrics.get("input_tokens"),
            "total_completion_tokens": metrics.get("output_tokens"),
            "total_steps": len(trace.get("steps") or []),
            "extra": {
                "total_tokens": metrics.get("total_tokens"),
                "llm_call_count": metrics.get("llm_call_count"),
                "tool_calls_count": metrics.get("tool_calls_count"),
                "time_taken_seconds": metrics.get("time_taken_seconds"),
            },
        }
    )


def _trace_refs_by_episode(scenario_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    refs: dict[str, dict[str, Any]] = {}
    for episode_result in scenario_result.get("episodes") or []:
        if not isinstance(episode_result, dict):
            continue
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


def _agent_payload(agent: Any, diagnosis: Any) -> dict[str, Any]:
    return {
        "name": str(getattr(diagnosis, "agent_name", None) or getattr(agent, "name", None) or "unknown"),
        "class": getattr(getattr(agent, "__class__", None), "__name__", type(agent).__name__),
    }


def _agent_model_payload(agent: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    provider = _first_agent_attr(agent, ("vendor", "provider"))
    model = _first_agent_attr(agent, ("model", "model_name"))
    runtime = _first_agent_attr(agent, ("runtime", "runtime_name"))
    if provider:
        payload["provider"] = provider
    if model:
        payload["model"] = model
    if runtime:
        payload["runtime"] = runtime
    return _jsonable(payload)


def _first_agent_attr(agent: Any, names: tuple[str, ...]) -> str | None:
    for candidate in _agent_candidates(agent):
        for name in names:
            value = getattr(candidate, name, None)
            if value is None or callable(value):
                continue
            text = str(value).strip()
            if text:
                return text
    return None


def _agent_candidates(agent: Any):
    seen: set[int] = set()
    stack = [agent]
    while stack:
        candidate = stack.pop(0)
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        yield candidate
        for attr in ("agent", "_agent", "wrapped_agent", "inner"):
            inner = getattr(candidate, attr, None)
            if inner is not None and id(inner) not in seen:
                stack.append(inner)


def _model_payload(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _jsonable(metadata[key])
        for key in ("provider", "model", "runtime")
        if key in metadata and metadata.get(key) is not None
    }


def _metrics_payload(
    metadata: dict[str, Any],
    diagnosis_payload: dict[str, Any],
    *,
    trace_recorder: AgentTraceRecorder | None = None,
    started_at: datetime,
    ended_at: datetime,
) -> dict[str, Any]:
    recorder_metrics = trace_recorder.metrics() if trace_recorder is not None else {}
    payload = {
        "time_taken_seconds": float(
            diagnosis_payload.get("time_taken_seconds") or max(0.0, (ended_at - started_at).total_seconds())
        ),
        "tool_calls_count": len(
            (trace_recorder.tool_calls() if trace_recorder is not None else [])
            or diagnosis_payload.get("tool_calls")
            or metadata.get("tool_calls")
            or []
        ),
    }
    for key in ("input_tokens", "output_tokens", "total_tokens", "llm_call_count"):
        if recorder_metrics.get(key):
            payload[key] = _jsonable(recorder_metrics[key])
        elif key in metadata:
            payload[key] = _jsonable(metadata[key])
    return payload


def _diagnosis_payload(diagnosis_payload: dict[str, Any]) -> dict[str, Any]:
    final = dict(diagnosis_payload)
    metadata = dict(final.get("metadata") or {})
    metadata.pop("trace", None)
    metadata.pop("trajectory", None)
    final["metadata"] = metadata
    return _jsonable(final)


def _diagnosis_error(diagnosis: Any, diagnosis_payload: dict[str, Any]) -> str | None:
    if diagnosis_payload.get("error"):
        return str(diagnosis_payload["error"])
    if getattr(diagnosis, "success", True) is False:
        findings = getattr(diagnosis, "findings", None) or {}
        if isinstance(findings, dict) and findings.get("error"):
            return str(findings["error"])
    return None


def _context_steps(diagnostic_context: Any) -> list[dict[str, Any]]:
    if diagnostic_context is None:
        return []
    payload = {
        "scenario_id": getattr(diagnostic_context, "scenario_id", None),
        "topology": getattr(diagnostic_context, "topology", None),
        "symptoms": getattr(diagnostic_context, "symptoms", None),
    }
    return [
        {
            "type": "message",
            "source": "user",
            "message": _atif_message_content(_jsonable(payload)),
            "is_copied_context": True,
        }
    ]


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _steps_from_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(tool_calls, list):
        return []
    return [
        {
            "index": index,
            "type": "tool_call",
            "name": (item if isinstance(item, dict) else {"tool": str(item)}).get("tool")
            or (item if isinstance(item, dict) else {}).get("name")
            or "tool",
            "args": (item if isinstance(item, dict) else {}).get("args")
            or (item if isinstance(item, dict) else {}).get("input")
            or {},
        }
        for index, item in enumerate(tool_calls, 1)
    ]


def _final_diagnosis_step(diagnosis_payload: dict[str, Any], *, ended_at: datetime) -> dict[str, Any]:
    final = _diagnosis_payload(diagnosis_payload)
    verdict = final.get("verdict") or ("error" if final.get("error") else "unknown")
    fault_type = final.get("fault_type")
    location = final.get("location") if isinstance(final.get("location"), dict) else {}
    location_text = ", ".join(str(value) for value in (location or {}).values() if value not in (None, ""))
    parts = [f"Final diagnosis: {verdict}"]
    if fault_type:
        parts.append(f"fault_type={fault_type}")
    if location_text:
        parts.append(f"location={location_text}")
    return {
        "type": "final_diagnosis",
        "source": "agent",
        "message": "; ".join(parts),
        "ended_at": _isoformat(ended_at),
        "extra": {"final": True, "diagnosis": _final_diagnosis_summary(final)},
    }


def _final_diagnosis_summary(final: dict[str, Any]) -> dict[str, Any]:
    return _jsonable(
        {
            key: final[key]
            for key in ("verdict", "fault_type", "location", "evidence", "confidence", "reasoning", "error")
            if key in final and final[key] not in (None, {}, [])
        }
    )


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _resolve_atif_path(run_path: Path, row: dict[str, Any]) -> Path:
    raw_path = row.get("atif_path")
    if raw_path and Path(raw_path).exists():
        return Path(raw_path)
    case_id = _safe_path_part(row.get("case_id") or "case")
    matches = sorted((run_path / "traces").glob(f"*/{case_id}/trajectory.atif.json"))
    if matches:
        return matches[0]
    worker = _safe_path_part(row.get("worker") or "worker")
    candidate = run_path / "traces" / worker / case_id / "trajectory.atif.json"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"ATIF trajectory not found for trace {row.get('trace_id')}")


def _matching_result_row(result_rows: list[dict[str, Any]], index_row: dict[str, Any]) -> dict[str, Any] | None:
    trace_id = index_row.get("trace_id")
    for row in result_rows:
        if row.get("trace_id") == trace_id:
            return row
    for row in result_rows:
        if row.get("scenario_id") == index_row.get("scenario_id") and row.get("episode_id") == index_row.get(
            "episode_id"
        ):
            return row
    return None


def _dataset_source(index_row: dict[str, Any]) -> str:
    scale = str(index_row.get("topology_scale") or "").strip()
    if scale and scale.lower() != "unknown":
        return f"netopsbench-{scale}"
    return "netopsbench"


def _job_config_model_name(index_row: dict[str, Any]) -> str | None:
    model = index_row.get("model")
    if not model:
        return None
    model_text = str(model)
    provider = str(index_row.get("provider") or "").strip()
    if provider and not model_text.startswith(f"{provider}/"):
        return f"{provider}/{model_text}"
    return model_text


def _attach_mean_reward_metrics(stats: JobStats, trial_results: list[TrialResult]) -> None:
    rewards_by_eval: dict[str, list[dict[str, float | int] | None]] = {}
    for trial_result in trial_results:
        agent_name = trial_result.agent_info.name
        model_name = trial_result.agent_info.model_info.name if trial_result.agent_info.model_info else None
        dataset_name = trial_result.source or "adhoc"
        evals_key = JobStats.format_agent_evals_key(agent_name, model_name, dataset_name)
        rewards = trial_result.verifier_result.rewards if trial_result.verifier_result else None
        rewards_by_eval.setdefault(evals_key, []).append(rewards)
    for evals_key, rewards in rewards_by_eval.items():
        eval_stats = stats.evals.get(evals_key)
        if eval_stats is not None:
            eval_stats.metrics = [_mean_reward_metric(rewards)]


def _mean_reward_metric(rewards: list[dict[str, float | int] | None]) -> dict[str, float | int]:
    reward_keys = sorted({key for reward in rewards if reward is not None for key in reward})
    if len(reward_keys) <= 1:
        values = [0 if reward is None else next(iter(reward.values()), 0) for reward in rewards]
        return {"mean": sum(values) / len(values) if values else 0.0}
    return {
        key: sum(0 if reward is None else reward.get(key, 0) for reward in rewards) / len(rewards)
        for key in reward_keys
    }


def _run_times(run_path: Path, index_rows: list[dict[str, Any]]) -> dict[str, str]:
    report_path = run_path / "report.json"
    if report_path.exists():
        try:
            summary = json.loads(report_path.read_text(encoding="utf-8")).get("summary") or {}
            if summary.get("started_at") and summary.get("completed_at"):
                return {
                    "started_at": _normalise_iso_z(summary["started_at"]),
                    "finished_at": _normalise_iso_z(summary["completed_at"]),
                }
        except Exception:
            pass
    starts = [str(value) for row in index_rows if (value := row.get("started_at"))]
    ends = [str(value) for row in index_rows if (value := row.get("ended_at"))]
    now = _isoformat(datetime.now(UTC))
    return {
        "started_at": _normalise_iso_z(min(starts) if starts else now),
        "finished_at": _normalise_iso_z(max(ends) if ends else now),
    }


def _normalise_iso_z(value: Any) -> str:
    text = str(value)
    return text[:-6] + "Z" if text.endswith("+00:00") else text


def _atif_source(step: dict[str, Any]) -> str:
    raw = str(step.get("source") or step.get("role") or step.get("type") or "agent").lower()
    if raw == "human":
        return "user"
    if raw in {"system", "user"}:
        return raw
    return "agent"


def _is_tool_step(step: dict[str, Any]) -> bool:
    if str(step.get("type") or "").lower() in {"tool", "tool_call"}:
        return True
    if step.get("tool_call_id"):
        return True
    return bool(step.get("name") and ("args" in step or "input" in step))


def _step_message(step: dict[str, Any]) -> Any:
    for key in ("message", "content", "completion", "prompt"):
        if step.get(key) is not None:
            return step[key]
    return None


def _atif_step_metrics(step: dict[str, Any]) -> dict[str, Any]:
    usage = step.get("usage") if isinstance(step.get("usage"), dict) else {}
    metrics: dict[str, Any] = {}
    if usage:
        metrics["prompt_tokens"] = usage.get("input_tokens") or usage.get("prompt_tokens")
        metrics["completion_tokens"] = usage.get("output_tokens") or usage.get("completion_tokens")
        if usage.get("total_tokens") is not None:
            metrics.setdefault("extra", {})["total_tokens"] = usage["total_tokens"]
    if step.get("duration_seconds") is not None:
        metrics.setdefault("extra", {})["duration_seconds"] = step["duration_seconds"]
    return {key: value for key, value in metrics.items() if value not in (None, {})}


def _atif_observation_content(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(_jsonable(value), sort_keys=True, default=str)


def _atif_message_content(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(_jsonable(value), sort_keys=True, default=str)


def _compact_extra(step: dict[str, Any]) -> dict[str, Any]:
    excluded = {
        "message",
        "content",
        "prompt",
        "completion",
        "reasoning_content",
        "tool_calls",
        "observation",
        "args",
        "input",
        "output",
        "usage",
        "extra",
    }
    extra = dict(step.get("extra") or {}) if isinstance(step.get("extra"), dict) else {}
    for key, value in step.items():
        if key not in excluded:
            extra[key] = value
    return extra


def _collapse_copied_context_steps(steps: list[Any]) -> list[Any]:
    collapsed: list[Any] = []
    saw_runtime_event = False
    seen_context: set[str] = set()
    for item in steps:
        if not isinstance(item, dict):
            collapsed.append(item)
            saw_runtime_event = True
            continue
        if item.get("is_copied_context"):
            fingerprint = _message_fingerprint(item)
            if saw_runtime_event or fingerprint in seen_context:
                continue
            copied = dict(item)
            copied.pop("is_copied_context", None)
            copied["is_initial_context"] = True
            collapsed.append(copied)
            seen_context.add(fingerprint)
            continue
        collapsed.append(item)
        saw_runtime_event = True
    return collapsed


def _message_fingerprint(step: dict[str, Any]) -> str:
    return json.dumps(
        _jsonable(
            {
                "type": step.get("type"),
                "source": step.get("source") or step.get("role"),
                "message": step.get("message") if "message" in step else step.get("content"),
                "tool_call_id": step.get("tool_call_id"),
            }
        ),
        sort_keys=True,
        default=str,
    )


def _episode_id_from_testcase(testcase_id: Any) -> str | None:
    if not testcase_id:
        return None
    text = str(testcase_id)
    return text.rsplit(":", 1)[1] if ":" in text else None


def _duration_seconds(started_at: Any, ended_at: Any) -> float | None:
    try:
        if not started_at or not ended_at:
            return None
        start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(ended_at).replace("Z", "+00:00"))
        return max(0.0, (end - start).total_seconds())
    except Exception:
        return None


def _error_stage(trace: dict[str, Any]) -> str | None:
    if not trace.get("error"):
        return None
    metadata = (trace.get("final_diagnosis") or {}).get("metadata") or {}
    return metadata.get("agent_failure_stage") or metadata.get("failure_stage") or "diagnose"


def _safe_path_part(value: Any) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(value))
    return safe[:120] or "unknown"


def _isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _to_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


__all__ = [
    "ATIF_SCHEMA_VERSION",
    "TraceWriter",
    "TraceWriteResult",
    "build_atif_payload",
    "build_trace_payload",
    "export_traces",
    "load_trace_index",
    "load_trace_results",
]
