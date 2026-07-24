from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.platform.runtime.manager import RuntimePool
from netopsbench.platform.session.context import build_worker_execution_context
from netopsbench.platform.session.dispatch import execute_on_runtime_pool
from netopsbench.platform.session.types import WorkerExecutionContext


class _FakeRunner:
    closed = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.results_dir = Path(".")
        self.evaluator = kwargs["evaluator"]

    def run_scenario(self, scenario, diagnosis_callback=None):
        return {
            "success": True,
            "scenario_id": scenario.scenario_id,
            "episode": {},
            "persist_results": self.kwargs.get("persist_results"),
        }

    def close(self):
        type(self).closed += 1


class _FakeEvaluator:
    _next_id = 0

    def __init__(self):
        type(self)._next_id += 1
        self.id = type(self)._next_id

    def generate_report(self, results, agent_name="unknown", topology_scale="unknown"):
        return {
            "summary": {
                "total_cases": len(results),
                "evaluator_id": self.id,
                "agent_name": agent_name,
                "topology_scale": topology_scale,
            },
            "detailed_results": [{"evaluator_id": item.evaluator_id} for item in results],
        }


def _scenario(scenario_id: str) -> ScenarioSpec:
    return ScenarioSpec(
        scenario_id=scenario_id,
        name=scenario_id,
        description="test",
        topology_scale="xs",
        episode=EpisodeSpec(
            episode_id=f"{scenario_id}-ep1",
            description="episode",
            fault_type="link_down",
            target_device="leaf1",
            target_interface="Ethernet1",
        ),
    )


def _worker(tmp_path: Path, index: int) -> RuntimeIdentity:
    topology_dir = tmp_path / f"worker-{index}"
    topology_dir.mkdir()
    return RuntimeIdentity.create(
        runtime_id="runtime-1",
        worker_id=f"worker-{index}",
        worker_index=index,
        lab_name=f"lab-{index}",
        topology_dir=topology_dir,
        mgmt_subnet=f"172.31.{index}.0/24",
        mgmt_network=f"clab-mgmt-lab-{index}",
        bucket=f"bucket-{index}",
    )


def test_worker_context_uses_lab_name_for_observability_topology_id(tmp_path):
    worker = _worker(tmp_path, 1)

    context = build_worker_execution_context(worker, Path(worker.topology_dir))

    assert context.topology_id == "lab-1"
    assert context.as_env()["NETOPSBENCH_TOPOLOGY_ID"] == "lab-1"


def test_worker_context_uses_explicit_identity_topology_id(tmp_path):
    worker = _worker(tmp_path, 1)
    worker = worker.model_copy(update={"topology_id": "runtime-observability-id"})

    context = build_worker_execution_context(worker, Path(worker.topology_dir))

    assert context.topology_id == "runtime-observability-id"


def test_worker_context_rejects_topology_directory_mismatch(tmp_path):
    worker = _worker(tmp_path, 1)

    with pytest.raises(ValueError, match="does not match runtime identity"):
        build_worker_execution_context(worker, tmp_path / "other")


def test_execute_on_runtime_pool_uses_per_worker_evaluators_and_session_raw_persistence(tmp_path, monkeypatch):
    import netopsbench.platform.session.dispatch as dispatch

    _FakeEvaluator._next_id = 0
    _FakeRunner.closed = 0
    runtime = RuntimePool(
        id="runtime-1",
        name="runtime-1",
        scale="xs",
        root_dir=tmp_path / "runtime",
        workers=[_worker(tmp_path, 1), _worker(tmp_path, 2)],
    )
    scenarios = [_scenario("scenario-1"), _scenario("scenario-2")]

    def score_episode(_scenario, scenario_result, evaluator, **_kwargs):
        assert scenario_result["persist_results"] is False
        return [SimpleNamespace(score=1.0, evaluator_id=evaluator.id)]

    monkeypatch.setattr(dispatch, "ScenarioExecutor", _FakeRunner)
    monkeypatch.setattr(dispatch, "_create_evaluator", _FakeEvaluator)
    monkeypatch.setattr(dispatch, "score_scenario_episode", score_episode)
    monkeypatch.setattr(dispatch, "build_runtime_diagnosis_callback", lambda *_args: lambda _payload: {})
    monkeypatch.setattr(
        dispatch,
        "build_worker_execution_context",
        lambda worker, topology_dir: WorkerExecutionContext(
            topology_dir=topology_dir,
            topology_id=f"topo-{worker.worker_index}",
            influxdb_bucket=worker.bucket,
        ),
    )
    monkeypatch.setattr(dispatch, "load_topology_metadata", lambda _topology_dir: None)

    result = execute_on_runtime_pool(
        scenarios=scenarios,
        runtime=runtime,
        agent=SimpleNamespace(name="agent"),
        raw_dir=tmp_path / "artifacts" / "raw",
    )

    summaries = result.scenarios
    assert [summary["scenario_id"] for summary in summaries] == ["scenario-1", "scenario-2"]
    assert [Path(summary["raw_result_path"]).exists() for summary in summaries] == [True, True]
    assert {item.evaluator_id for item in result.evaluations} == {1, 2}
    assert [summary["worker_id"] for summary in result.workers] == ["worker-1", "worker-2"]
    assert _FakeRunner.closed == 2


def test_execute_on_runtime_pool_rejects_scenario_scale_mismatch(tmp_path):
    runtime = RuntimePool(
        id="runtime-1",
        name="runtime-1",
        scale="small",
        root_dir=tmp_path / "runtime",
        workers=[_worker(tmp_path, 1)],
    )

    with pytest.raises(ValueError, match="does not match runtime scale"):
        execute_on_runtime_pool(
            scenarios=[_scenario("scenario-1")],
            runtime=runtime,
            agent=SimpleNamespace(name="agent"),
            raw_dir=tmp_path / "raw",
        )


def test_cleanup_failure_skips_only_that_workers_remaining_cases(tmp_path, monkeypatch):
    import netopsbench.platform.session.dispatch as dispatch

    class CleanupAwareRunner(_FakeRunner):
        def run_scenario(self, scenario, diagnosis_callback=None):
            cleanup_success = scenario.id != "scenario-1"
            return {
                "success": cleanup_success,
                "scenario_id": scenario.id,
                "episode": {},
                "cleanup": {"success": cleanup_success},
            }

    runtime = RuntimePool(
        id="runtime-1",
        name="runtime-1",
        scale="xs",
        root_dir=tmp_path / "runtime",
        workers=[_worker(tmp_path, 1), _worker(tmp_path, 2)],
    )
    scenarios = [_scenario(f"scenario-{index}") for index in range(1, 5)]

    monkeypatch.setattr(dispatch, "ScenarioExecutor", CleanupAwareRunner)
    monkeypatch.setattr(dispatch, "_create_evaluator", _FakeEvaluator)
    monkeypatch.setattr(
        dispatch,
        "score_scenario_episode",
        lambda _scenario, _result, evaluator, **_kwargs: [SimpleNamespace(score=1.0, evaluator_id=evaluator.id)],
    )
    monkeypatch.setattr(dispatch, "build_runtime_diagnosis_callback", lambda *_args: lambda _payload: {})
    monkeypatch.setattr(
        dispatch,
        "build_worker_execution_context",
        lambda worker, topology_dir: WorkerExecutionContext(
            topology_dir=topology_dir,
            topology_id=f"topo-{worker.worker_index}",
            influxdb_bucket=worker.bucket,
        ),
    )
    monkeypatch.setattr(dispatch, "load_topology_metadata", lambda _topology_dir: None)

    result = execute_on_runtime_pool(
        scenarios=scenarios,
        runtime=runtime,
        agent=SimpleNamespace(name="agent"),
        raw_dir=tmp_path / "raw",
    )

    summaries = {item["scenario_id"]: item for item in result.scenarios}
    assert summaries["scenario-1"]["status"] == "failed"
    assert summaries["scenario-1"]["failure_stage"] == "cleanup"
    assert summaries["scenario-3"]["status"] == "skipped_infrastructure_failure"
    assert summaries["scenario-2"]["status"] == "completed"
    assert summaries["scenario-4"]["status"] == "completed"
    assert len(result.evaluations) == 3
    assert result.workers == [
        {
            "worker_id": "worker-1",
            "worker_name": "worker-1",
            "lab_name": "lab-1",
            "scenario_count": 2,
            "executed_count": 1,
            "skipped_count": 1,
            "success": False,
        },
        {
            "worker_id": "worker-2",
            "worker_name": "worker-2",
            "lab_name": "lab-2",
            "scenario_count": 2,
            "executed_count": 2,
            "skipped_count": 0,
            "success": True,
        },
    ]
