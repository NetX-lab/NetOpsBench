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
        self.evaluator = kwargs["evaluator"]

    def run_scenario(self, scenario, diagnosis_callback=None):
        return {
            "success": True,
            "scenario_id": scenario.scenario_id,
            "episode": {},
        }

    def final_calibration(self, scenario, **kwargs):
        return {"success": True}

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
        metadata={"recovery_policy": {"version": 3}},
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
    assert context.topology_dir == worker.topology_dir
    assert context.influxdb_bucket == worker.bucket


@pytest.mark.parametrize(
    ("domain", "phase", "cleanup", "valid", "cancelled", "cleanup_errors", "continues"),
    [
        ("agent", "diagnosis", True, True, False, [], True),
        ("agent", "limits", True, True, False, [], True),
        ("protocol", "diagnosis", True, True, False, [], True),
        ("infrastructure", "observation", True, False, False, [], False),
        ("agent", "diagnosis", False, True, False, [], False),
        ("agent", "diagnosis", None, True, False, [], False),
        ("agent", "diagnosis", True, True, True, [], False),
        ("agent", "diagnosis", True, True, False, ["container cleanup failed"], False),
        ("agent", "observation", True, True, False, [], False),
    ],
)
def test_acceptance_continues_only_after_recovered_agent_failure(
    tmp_path, monkeypatch, domain, phase, cleanup, valid, cancelled, cleanup_errors, continues
):
    import threading

    import netopsbench.platform.session.dispatch as dispatch

    cancel = threading.Event()
    executed, checkpoints, built, closed, quarantined = [], [], [], [], []

    class Runner:
        evaluator = None

        def run_scenario(self, scenario, diagnosis_callback=None):
            executed.append(scenario.id)
            if scenario.id == "first":
                if cancelled:
                    cancel.set()
                return {
                    "success": False,
                    "case_valid": valid,
                    "cleanup": {} if cleanup is None else {"success": cleanup},
                    "episode": {
                        "execution": {"failure": {"domain": domain, "phase": phase}},
                        "diagnosis": {"metadata": {"cleanup_errors": cleanup_errors}},
                    },
                }
            return {"success": True, "case_valid": True, "cleanup": {"success": True}, "episode": {}}

        def final_calibration(self, scenario, **kwargs):
            return {"success": True}

        def close(self):
            closed.append(True)

    def build(*args, **kwargs):
        built.append(True)
        return Runner()

    monkeypatch.setattr(dispatch, "_build_scenario_executor", build)
    monkeypatch.setattr(dispatch, "build_worker_execution_context", lambda *a: SimpleNamespace(topology_dir=tmp_path))
    monkeypatch.setattr(dispatch, "require_scenario_topology", lambda *a: None)
    monkeypatch.setattr(dispatch, "build_runtime_diagnosis_callback", lambda *a, **kw: None)
    runtime = SimpleNamespace(id="runtime", mark_quarantined=lambda *a: quarantined.append(a))
    result = dispatch._run_worker(
        runtime=runtime,
        agent=None,
        raw_dir=tmp_path,
        trace_writer=None,
        fault_registry=None,
        minimum_baseline_seconds=None,
        scale_registry=None,
        worker=SimpleNamespace(worker_id="worker-1", lab_name="lab", topology_dir=tmp_path),
        scenarios=[_scenario("first"), _scenario("second")],
        control=dispatch.ExecutionControl(
            on_case=lambda summary, *args: checkpoints.append(summary), stop_on_failure=True, cancel_event=cancel
        ),
    )
    assert executed == (["first", "second"] if continues else ["first"])
    assert len(built) == len(closed) == 1
    assert len(checkpoints) == len(executed)
    assert dispatch.is_recovered_case_failure(checkpoints[0]) is continues
    assert bool(quarantined) is (cleanup is False)
    assert result[2]["success"] is False
    assert result[0] == []  # Failed agent execution must not create a score.


def test_worker_context_uses_explicit_identity_topology_id(tmp_path):
    worker = _worker(tmp_path, 1)
    worker = worker.model_copy(update={"topology_id": "runtime-observability-id"})

    context = build_worker_execution_context(worker, Path(worker.topology_dir))

    assert context.topology_id == "runtime-observability-id"


@pytest.mark.parametrize(
    ("recovery_verified", "effects_only", "continues"),
    [(True, False, True), (False, False, False), (True, True, False)],
)
def test_invalid_effect_continues_only_after_verified_health(
    tmp_path, monkeypatch, recovery_verified, effects_only, continues
):
    import netopsbench.platform.session.dispatch as dispatch

    class Runner(_FakeRunner):
        def run_scenario(self, scenario, diagnosis_callback=None):
            if scenario.id == "invalid":
                return {
                    "success": False,
                    "case_valid": False,
                    "error": "Owned ACL rule has no verified hits",
                    "episode": {"effect_audit": {"valid": False}},
                    "cleanup": {
                        "success": True,
                        **({"validated_baseline": {"duration_seconds": 42}} if recovery_verified else {}),
                    },
                }
            return {"success": True, "case_valid": True, "cleanup": {"success": True}, "episode": {}}

    runtime = RuntimePool(
        id="runtime-1",
        name="runtime-1",
        scale="xs",
        root_dir=tmp_path / "runtime",
        workers=[_worker(tmp_path, 1)],
        state="warm",
    )
    monkeypatch.setattr(dispatch, "ScenarioExecutor", Runner)
    monkeypatch.setattr(dispatch, "_create_evaluator", _FakeEvaluator)
    monkeypatch.setattr(dispatch, "build_runtime_diagnosis_callback", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "load_topology_metadata", lambda *a: None)
    monkeypatch.setattr(dispatch, "require_scenario_topology", lambda *a: None)
    result = execute_on_runtime_pool(
        runtime=runtime,
        scenarios=[_scenario("invalid"), _scenario("next")],
        agent=None,
        raw_dir=tmp_path / "raw",
        control=dispatch.ExecutionControl(stop_on_failure=True, effects_only=effects_only),
    )
    first = result.scenarios[0]
    assert first["status"] == "invalid" and first["case_valid"] is False
    assert dispatch.is_recovered_case_failure(first) is continues
    assert result.workers[0]["executed_count"] == (2 if continues else 1)
    assert result.scenarios[1]["status"] == ("completed" if continues else "skipped_execution_failure")
    assert result.evaluations == []
    assert runtime.state == "warm"


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
        state="warm",
    )
    scenarios = [_scenario("scenario-1"), _scenario("scenario-2")]

    def score_episode(_scenario, scenario_result, evaluator, **_kwargs):
        return [SimpleNamespace(score=1.0, evaluator_id=evaluator.id)]

    monkeypatch.setattr(dispatch, "ScenarioExecutor", _FakeRunner)
    monkeypatch.setattr(dispatch, "_create_evaluator", _FakeEvaluator)
    monkeypatch.setattr(dispatch, "score_scenario_episode", score_episode)
    monkeypatch.setattr(dispatch, "build_runtime_diagnosis_callback", lambda *_args, **_kwargs: lambda _payload: {})
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
    validated = []
    monkeypatch.setattr(
        dispatch,
        "require_scenario_topology",
        lambda scenario, topology_dir: validated.append((scenario.id, Path(topology_dir).name)),
    )

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
    assert sorted(validated) == [("scenario-1", "worker-1"), ("scenario-2", "worker-2")]


def test_execute_on_runtime_pool_rejects_scenario_scale_mismatch(tmp_path):
    runtime = RuntimePool(
        id="runtime-1",
        name="runtime-1",
        scale="medium",
        root_dir=tmp_path / "runtime",
        workers=[_worker(tmp_path, 1)],
        state="warm",
    )

    with pytest.raises(ValueError, match="does not match runtime scale"):
        execute_on_runtime_pool(
            scenarios=[_scenario("scenario-1")],
            runtime=runtime,
            agent=SimpleNamespace(name="agent"),
            raw_dir=tmp_path / "raw",
        )


def test_execute_on_runtime_pool_rejects_non_warm_or_quarantined_runtime(tmp_path):
    runtime = RuntimePool(
        id="runtime-1",
        name="runtime-1",
        scale="xs",
        root_dir=tmp_path / "runtime",
        workers=[_worker(tmp_path, 1)],
        state="deployed",
    )

    with pytest.raises(RuntimeError, match="not eligible"):
        execute_on_runtime_pool(
            scenarios=[_scenario("scenario-1")],
            runtime=runtime,
            agent=SimpleNamespace(name="agent"),
            raw_dir=tmp_path / "raw",
        )

    runtime.state = "warm"
    runtime.metadata["quarantined"] = True
    with pytest.raises(RuntimeError, match="quarantined=True"):
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
                "case_valid": True,
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
        state="warm",
    )
    scenarios = [_scenario(f"scenario-{index}") for index in range(1, 5)]

    monkeypatch.setattr(dispatch, "ScenarioExecutor", CleanupAwareRunner)
    monkeypatch.setattr(dispatch, "_create_evaluator", _FakeEvaluator)
    monkeypatch.setattr(
        dispatch,
        "score_scenario_episode",
        lambda _scenario, _result, evaluator, **_kwargs: [SimpleNamespace(score=1.0, evaluator_id=evaluator.id)],
    )
    monkeypatch.setattr(dispatch, "build_runtime_diagnosis_callback", lambda *_args, **_kwargs: lambda _payload: {})
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
    monkeypatch.setattr(dispatch, "require_scenario_topology", lambda _scenario, _topology_dir: None)

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
            "final_calibration": {"success": True},
        },
    ]
    assert runtime.metadata["quarantined"] is True
    assert runtime.state == "quarantined"
    assert runtime.metadata["quarantine_reason"] == "scenario_cleanup_failure"
    assert runtime.metadata["quarantined_workers"] == ["worker-1"]
    persisted = (runtime.root_dir / "runtime.json").read_text(encoding="utf-8")
    assert '"quarantined": true' in persisted


def test_invalid_case_with_cleanup_failure_still_quarantines_runtime(tmp_path, monkeypatch):
    import netopsbench.platform.session.dispatch as dispatch

    class InvalidCleanupRunner(_FakeRunner):
        def run_scenario(self, scenario, diagnosis_callback=None):
            return {
                "success": False,
                "case_valid": False,
                "scenario_id": scenario.id,
                "episode": None,
                "cleanup": {"success": False, "status": "terminal_recovery_failure"},
                "cleanup_failure": {"domain": "cleanup", "message": "parking namespace is missing"},
            }

    runtime = RuntimePool(
        id="runtime-1",
        name="runtime-1",
        scale="xs",
        root_dir=tmp_path / "runtime",
        workers=[_worker(tmp_path, 1)],
        state="warm",
    )
    monkeypatch.setattr(dispatch, "ScenarioExecutor", InvalidCleanupRunner)
    monkeypatch.setattr(dispatch, "_create_evaluator", _FakeEvaluator)
    monkeypatch.setattr(dispatch, "build_runtime_diagnosis_callback", lambda *_args, **_kwargs: lambda _payload: {})
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
    monkeypatch.setattr(dispatch, "require_scenario_topology", lambda _scenario, _topology_dir: None)

    result = execute_on_runtime_pool(
        scenarios=[_scenario("scenario-1")],
        runtime=runtime,
        agent=SimpleNamespace(name="agent"),
        raw_dir=tmp_path / "raw",
    )

    summary = result.scenarios[0]
    assert summary["status"] == "invalid"
    assert summary["failure_stage"] == "infrastructure"
    assert summary["cleanup_failed"] is True
    assert result.evaluations == []
    assert runtime.state == "quarantined"
    assert runtime.metadata["quarantined_workers"] == ["worker-1"]


@pytest.mark.parametrize("failed", [False, True])
def test_final_calibration_is_reported_and_failure_quarantines_without_rescoring(tmp_path, monkeypatch, failed):
    import netopsbench.platform.session.dispatch as dispatch
    from netopsbench.models.recovery import RecoveryPolicy

    calls, quarantined = [], []
    score = object()

    class Runner:
        evaluator = object()

        def run_scenario(self, scenario, diagnosis_callback=None):
            return {"success": True, "case_valid": True, "cleanup": {"success": True}, "episode": {}}

        def final_calibration(self, scenario, **kwargs):
            calls.append("calibration")
            if failed:
                raise RuntimeError("late loss")
            return {"success": True}

        def close(self):
            calls.append("close")

    monkeypatch.setattr(dispatch, "_build_scenario_executor", lambda *a, **k: Runner())
    monkeypatch.setattr(dispatch, "build_worker_execution_context", lambda *a: SimpleNamespace(topology_dir=tmp_path))
    monkeypatch.setattr(dispatch, "require_scenario_topology", lambda *a: None)
    monkeypatch.setattr(dispatch, "build_runtime_diagnosis_callback", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "score_scenario_episode", lambda *a, **k: calls.append("score") or [score])
    case = _scenario("one")
    case.metadata["recovery_policy"] = RecoveryPolicy().model_dump()
    evaluations, summaries, worker = dispatch._run_worker(
        runtime=SimpleNamespace(id="runtime", mark_quarantined=lambda *a: quarantined.append(a)),
        agent=None,
        raw_dir=tmp_path,
        trace_writer=None,
        fault_registry=None,
        minimum_baseline_seconds=None,
        scale_registry=None,
        worker=SimpleNamespace(worker_id="worker-1", lab_name="lab", topology_dir=tmp_path),
        scenarios=[case],
    )
    assert evaluations == [score]
    assert calls == ["score", "calibration", "close"]
    assert worker["success"] is (not failed)
    assert bool(quarantined) is failed
    assert summaries[0]["status"] == "completed"
    assert (tmp_path / "worker-1" / "final_calibration.json").exists()
