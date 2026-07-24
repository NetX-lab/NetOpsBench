"""Harbor viewer export for persisted NetOpsBench traces."""

from __future__ import annotations

import json
import shutil
import uuid
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

from .trace_utils import isoformat as _isoformat
from .trace_utils import load_jsonl as _load_jsonl
from .trace_utils import safe_path_part as _safe_path_part
from .trace_utils import to_json as _to_json


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
    trace_part = _safe_path_part(str(index_row.get("trace_id") or "trace").rsplit(":", 1)[-1])
    trial_name = _safe_path_part(f"{scenario_id}__{case_id}__{trace_part}")
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


def _resolve_atif_path(run_path: Path, row: dict[str, Any]) -> Path:
    trace_id = str(row.get("trace_id") or "")
    if not trace_id:
        raise FileNotFoundError("ATIF trajectory cannot be resolved without trace_id")
    raw_path = row.get("atif_path")
    if raw_path:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = run_path / candidate
        if candidate.exists() and _atif_matches_trace(candidate, trace_id):
            return candidate
    case_id = _safe_path_part(row.get("case_id") or "case")
    worker = _safe_path_part(row.get("worker") or "worker")
    case_dir = run_path / "traces" / worker / case_id
    trace_suffix = _safe_path_part(trace_id.rsplit(":", 1)[-1])
    candidate = case_dir / f"trajectory-{trace_suffix}.atif.json"
    if candidate.exists() and _atif_matches_trace(candidate, trace_id):
        return candidate
    matches = [path for path in case_dir.glob("trajectory-*.atif.json") if _atif_matches_trace(path, trace_id)]
    if len(matches) == 1:
        return matches[0]
    raise FileNotFoundError(f"ATIF trajectory not found for trace {trace_id}")


def _atif_matches_trace(path: Path, trace_id: str) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get("trajectory_id") == trace_id


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


__all__ = ["export_traces", "load_trace_index", "load_trace_results"]
