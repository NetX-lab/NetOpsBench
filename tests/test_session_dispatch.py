from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from netopsbench.platform.runtime.manager import RuntimePool
from netopsbench.platform.scenario.models import Episode, Scenario
from netopsbench.platform.session.dispatch import execute_on_runtime_pool
from netopsbench.platform.session.types import ScenarioExecutionRef, WorkerExecutionContext
from netopsbench.platform.worker.pool import WorkerSpec


class _FakeRunner:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.results_dir = Path(".")

    def run_scenario(self, scenario, diagnosis_callback=None):
        return {
            "success": True,
            "scenario_id": scenario.scenario_id,
            "episodes": [],
            "persist_results": self.kwargs.get("persist_results"),
        }


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


def _scenario_ref(scenario_id: str) -> ScenarioExecutionRef:
    scenario = Scenario(
        scenario_id=scenario_id,
        name=scenario_id,
        description="test",
        topology_scale="xs",
        traffic_profile="standard",
        episodes=[
            Episode(
                episode_id=f"{scenario_id}-ep1",
                description="episode",
                fault_type="link_down",
                target_device="leaf1",
                target_interface="Ethernet1",
            )
        ],
    )
    return ScenarioExecutionRef.from_scenario(scenario)


def _worker(tmp_path: Path, index: int) -> WorkerSpec:
    topology_dir = tmp_path / f"worker-{index}"
    topology_dir.mkdir()
    return WorkerSpec(
        id=f"worker-{index}",
        name=f"worker-{index}",
        root_dir=topology_dir,
        index=index,
        lab_name=f"lab-{index}",
        topology_dir=str(topology_dir),
        mgmt_subnet=f"172.31.{index}.0/24",
        bucket=f"bucket-{index}",
        shard_dir=str(topology_dir / "scenarios"),
        report_path=str(topology_dir / "report.json"),
        log_path=str(topology_dir / "worker.log"),
        deploy_log_path=str(topology_dir / "deploy.log"),
    )


def test_execute_on_runtime_pool_uses_per_worker_evaluators_and_session_raw_persistence(tmp_path):
    _FakeEvaluator._next_id = 0
    runtime = RuntimePool(
        id="runtime-1",
        name="runtime-1",
        scale="xs",
        root_dir=tmp_path / "runtime",
        workers=[_worker(tmp_path, 1), _worker(tmp_path, 2)],
    )
    scenarios = [_scenario_ref("scenario-1"), _scenario_ref("scenario-2")]
    created_reports = []

    def score_fault_episodes(_scenario, scenario_result, evaluator, **_kwargs):
        assert scenario_result["persist_results"] is False
        return [SimpleNamespace(score=1.0, evaluator_id=evaluator.id)]

    def create_run_report(**kwargs):
        created_reports.append(kwargs)
        return {
            "status": "completed",
            "summary": {"status": "completed"},
            "raw": {"status": "completed"},
            "scenario_summaries": kwargs["scenario_summaries"],
            "aggregate_report": kwargs["aggregate_report"],
        }

    result = execute_on_runtime_pool(
        run_id="run-1",
        mode="suite",
        scenarios=scenarios,
        runtime=runtime,
        agent=SimpleNamespace(name="agent"),
        artifact_dir=tmp_path / "artifacts",
        raw_dir=tmp_path / "artifacts" / "raw",
        report_path=tmp_path / "artifacts" / "report.json",
        metadata_path=tmp_path / "artifacts" / "metadata.json",
        runtime_owner="platform",
        teardown="skipped",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at_factory=lambda: datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        scenario_runner_cls=_FakeRunner,
        evaluator_factory=_FakeEvaluator,
        score_fault_episodes=score_fault_episodes,
        diagnosis_callback_builder=lambda *_args: lambda _payload: {},
        worker_context_builder=lambda worker, topology_dir: WorkerExecutionContext(
            topology_dir=topology_dir,
            topology_id=f"topo-{worker.index}",
            influxdb_bucket=worker.bucket,
        ),
        topology_metadata_loader=lambda _topology_dir: None,
        create_run_report=create_run_report,
        save_run_report=lambda _payload, _path: None,
        save_run_metadata=lambda *_args, **_kwargs: None,
        build_run_handle=lambda **kwargs: kwargs,
        run_handle_adapter=lambda payload: payload,
        artifact_manager=SimpleNamespace(),
    )

    summaries = created_reports[0]["scenario_summaries"]
    assert [summary["scenario_id"] for summary in summaries] == ["scenario-1", "scenario-2"]
    assert [Path(summary["raw_result_path"]).exists() for summary in summaries] == [True, True]

    detailed = created_reports[0]["aggregate_report"]["detailed_results"]
    assert {item["evaluator_id"] for item in detailed} == {1, 2}
    assert created_reports[0]["aggregate_report"]["summary"]["evaluator_id"] == 3
    assert result["status"] == "completed"
