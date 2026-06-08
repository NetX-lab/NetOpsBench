import inspect
import json
import os
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from netopsbench.sdk.agents import DiagnosisResult
from netopsbench.sdk.core import NetOpsBench
from netopsbench.sdk.reports import BenchmarkReport, RunHandle
from netopsbench.sdk.scenarios import ScenarioManager
from netopsbench.sdk.sessions import SessionManager


class DummyAgent:
    def diagnose(self, context):
        return DiagnosisResult(
            agent_name="dummy",
            verdict="fault_detected",
            findings={
                "fault_type": "link_down",
                "location": {"device": "leaf1", "interface": "Ethernet1"},
                "evidence": ["fake-evidence"],
            },
            confidence=0.9,
            reasoning="fake diagnosis",
            metadata={},
        )


class RecordingAgent:
    def __init__(self):
        self.contexts = []

    def diagnose(self, context):
        self.contexts.append(context)
        return DiagnosisResult(
            agent_name="recording",
            verdict="inconclusive",
            findings={"fault_type": None, "location": {}, "evidence": []},
            confidence=0.0,
            reasoning="",
            metadata={},
        )


class _FakeEvalResult:
    def __init__(self, score=1.0):
        self.score = score


class _FakeEvaluator:
    def generate_report(self, results, agent_name="unknown", topology_scale="unknown"):
        average = round(sum(item.score for item in results) / len(results), 3) if results else 0.0
        return {
            "summary": {"total_cases": len(results), "average_score": average},
            "detailed_results": [{"score": item.score} for item in results],
        }


def _install_real_runtime_mocks(monkeypatch):
    import netopsbench.platform.session.orchestrator as sessions_mod

    class FakeScenarioExecutor:
        def __init__(
            self,
            topology_dir,
            topology_metadata=None,
            baseline_wait_seconds=5,
            post_recovery_wait_seconds=2,
            skip_none_episodes=False,
            influxdb_url=None,
            influxdb_token=None,
            influxdb_org=None,
            influxdb_bucket=None,
            topology_id=None,
        ):
            self.topology_dir = topology_dir
            self.topology_metadata = topology_metadata
            self.results_dir = None

        def run_scenario(self, scenario, diagnosis_callback=None):
            diagnosis = None
            if diagnosis_callback is not None:
                diagnosis = diagnosis_callback(
                    {
                        "description": "Inject link_down on leaf1",
                        "episode": {
                            "episode_id": "ep1",
                            "fault_type": "link_down",
                            "target_device": "leaf1",
                            "target_interface": "Ethernet1",
                        },
                        "observations": {"start_time": "2026-01-01T00:00:00Z", "end_time": "2026-01-01T00:01:00Z"},
                    }
                )
            return {
                "success": True,
                "result_file": str(Path(self.results_dir or ".") / f"{scenario.scenario_id}.json"),
                "episodes": [
                    {
                        "episode": {
                            "episode_id": "ep1",
                            "fault_type": "link_down",
                            "target_device": "leaf1",
                            "target_interface": "Ethernet1",
                        },
                        "diagnosis": diagnosis,
                    }
                ],
            }

    monkeypatch.setattr(sessions_mod, "ScenarioExecutor", FakeScenarioExecutor)
    monkeypatch.setattr(sessions_mod, "Evaluator", _FakeEvaluator)
    monkeypatch.setattr(sessions_mod, "score_scenario_fault_episodes", lambda *args, **kwargs: [_FakeEvalResult(1.0)])
    monkeypatch.setattr(sessions_mod, "_build_toolkit_for_topology", lambda topology_dir: object())
    monkeypatch.setattr(
        sessions_mod,
        "build_topology_snapshot",
        lambda toolkit: {"devices": {"spines": [], "leafs": [], "clients": []}, "links": []},
    )


def _install_platform_runtime_mocks(monkeypatch):
    import netopsbench.platform.runtime.manager as runtimes_mod

    def fake_provision(self, *, scale, workers=1, name=None, root_dir=None):
        runtime = self._build_runtime(scale=scale, workers=workers, name=name, root_dir=root_dir)
        runtime.metadata["provisioning_mode"] = "worker_pool"
        runtime.state = "deployed"
        for worker in runtime.workers:
            worker_dir = Path(worker.topology_dir or worker.root_dir)
            worker_dir.mkdir(parents=True, exist_ok=True)
            (worker_dir / "topology.json").write_text(
                '{"name":"%s"}' % (worker.lab_name or worker.name), encoding="utf-8"
            )
        runtime._write_metadata()
        return runtime

    monkeypatch.setattr(runtimes_mod.RuntimeManager, "provision", fake_provision)


def _make_scenario(*, scenario_id: str, scale: str = "xs"):
    return ScenarioManager().create(
        id=scenario_id,
        name=f"Scenario {scenario_id}",
        scale=scale,
        episodes=[
            {
                "episode_id": f"{scenario_id}-ep1",
                "fault_type": "link_down",
                "target_device": "leaf1",
                "target_interface": "Ethernet1",
            }
        ],
        metadata={"expected_diagnosis": "link_down", "difficulty": "easy"},
    )


def test_run_scenario_accepts_path_and_returns_real_run_handle(tmp_path, monkeypatch):
    _install_real_runtime_mocks(monkeypatch)
    _install_platform_runtime_mocks(monkeypatch)
    bench = NetOpsBench(workspace=str(tmp_path))
    assert isinstance(bench.sessions, SessionManager)
    assert bench.sessions.__class__.__module__ == "netopsbench.sdk.sessions"
    scenario = _make_scenario(scenario_id="scenario-1")
    scenario_path = tmp_path / "scenario-1.yaml"
    ScenarioManager().save(scenario, scenario_path)

    run = bench.sessions.run_scenario(
        scenario=scenario_path,
        agent=DummyAgent(),
        scale="xs",
        workers=1,
        root_dir=tmp_path / "custom-runtimes",
        keep_runtime=True,
        artifacts_dir=tmp_path / "artifacts",
    )

    assert isinstance(run, RunHandle)
    assert re.fullmatch(r"run-\d{8}T\d{6}Z", run.id)
    assert run.mode == "scenario"
    assert run.status == "completed"
    assert isinstance(run.started_at, datetime)
    assert isinstance(run.completed_at, datetime)
    assert run.runtime_id == f"{run.id}-runtime"
    assert run.artifact_dir == str(tmp_path / "artifacts" / run.id)
    assert run.scenario_ids == ["scenario-1"]
    assert Path(run.artifact_dir).exists()
    report = run.report()
    assert isinstance(report, BenchmarkReport)
    assert report.raw["status"] == "completed"
    assert report.summary["runtime_id"] == f"{run.id}-runtime"
    assert report.raw["runtime_owner"] == "platform"
    assert report.raw["execution"] == "real_runtime_runner"


def test_run_scenario_uses_runtime_pool_semantics_even_for_single_scenario(tmp_path, monkeypatch):
    _install_real_runtime_mocks(monkeypatch)
    _install_platform_runtime_mocks(monkeypatch)
    bench = NetOpsBench(workspace=str(tmp_path))
    scenario = _make_scenario(scenario_id="scenario-pool")

    run = bench.sessions.run_scenario(
        scenario=scenario,
        agent=DummyAgent(),
        workers=2,
        keep_runtime=True,
    )

    runtime = bench.runtimes.get(run.runtime_id)
    assert runtime is not None
    assert len(runtime.workers) == 2
    report = run.wait()
    assert report.summary["runtime_id"] == run.runtime_id
    assert report.raw["runtime_owner"] == "platform"
    assert report.raw["execution"] == "real_runtime_runner"


def test_run_on_runtime_scenario_supports_handle_and_path_without_teardown(tmp_path, monkeypatch):
    _install_real_runtime_mocks(monkeypatch)
    bench = NetOpsBench(workspace=str(tmp_path))
    scenario = _make_scenario(scenario_id="runtime-scenario")
    scenario_path = tmp_path / "runtime-scenario.yaml"
    ScenarioManager().save(scenario, scenario_path)
    runtime = bench.runtimes.create(scale="xs", workers=1, name="existing-runtime")

    first = bench.sessions.run_on_runtime_scenario(
        scenario=scenario,
        runtime=runtime,
        agent=DummyAgent(),
        artifacts_dir=tmp_path / "existing-runs",
    )
    second = bench.sessions.run_on_runtime_scenario(
        scenario=scenario_path,
        runtime=runtime,
        agent=DummyAgent(),
        artifacts_dir=tmp_path / "existing-runs",
    )

    assert first.runtime_id == "existing-runtime"
    assert second.runtime_id == "existing-runtime"
    assert bench.runtimes.get("existing-runtime") is not None
    assert (tmp_path / ".netopsbench" / "runtimes" / "existing-runtime").exists()


def test_run_on_runtime_suite_does_not_teardown_user_runtime(tmp_path, monkeypatch):
    _install_real_runtime_mocks(monkeypatch)
    bench = NetOpsBench(workspace=str(tmp_path))
    runtime = bench.runtimes.create(scale="xs", workers=1, name="shared-runtime")
    scenario_dir = tmp_path / "suite"
    scenario_dir.mkdir()
    ScenarioManager().save(_make_scenario(scenario_id="suite-1"), scenario_dir / "suite-1.yaml")
    ScenarioManager().save(_make_scenario(scenario_id="suite-2"), scenario_dir / "suite-2.yaml")

    run = bench.sessions.run_on_runtime_suite(
        scenarios=scenario_dir,
        runtime=runtime,
        agent=DummyAgent(),
        artifacts_dir=tmp_path / "runtime-suite-runs",
    )

    assert run.mode == "suite"
    assert run.runtime_id == "shared-runtime"
    assert run.scenario_ids == ["suite-1", "suite-2"]
    assert bench.runtimes.get("shared-runtime") is not None
    assert (tmp_path / ".netopsbench" / "runtimes" / "shared-runtime").exists()
    report = run.report()
    assert isinstance(report, BenchmarkReport)
    assert report.raw["runtime_owner"] == "user"
    assert report.raw["teardown"] == "skipped"
    assert report.raw["execution"] == "real_runtime_runner"


def test_runtime_agent_context_is_sanitized_and_no_ground_truth_leak(tmp_path, monkeypatch):
    _install_real_runtime_mocks(monkeypatch)
    bench = NetOpsBench(workspace=str(tmp_path))
    runtime = bench.runtimes.create(scale="xs", workers=1, name="ctx-runtime")
    scenario = _make_scenario(scenario_id="ctx-scenario")

    class CaptureAgent:
        def __init__(self):
            self.context = None

        def diagnose(self, context):
            self.context = context
            return DiagnosisResult(
                agent_name="capture-agent",
                verdict="inconclusive",
                findings={"evidence": ["captured"]},
                confidence=0.1,
                reasoning="captured context",
                metadata={},
            )

    agent = CaptureAgent()
    run = bench.sessions.run_on_runtime_scenario(
        scenario=scenario,
        runtime=runtime,
        agent=agent,
        artifacts_dir=tmp_path / "ctx-runs",
    )
    assert run.status == "completed"
    assert agent.context is not None
    assert agent.context.ground_truth is None
    assert agent.context.scenario_id.startswith("case-")
    assert "link_down" not in agent.context.scenario_id

    episode_payload = (agent.context.symptoms or {}).get("episode") or {}
    assert "fault_type" not in episode_payload
    assert "target_device" not in episode_payload
    assert "target_interface" not in episode_payload
    assert (agent.context.symptoms or {}).get("observations") is not None


def test_runtime_trace_metadata_is_persisted_only_as_sidecar(tmp_path, monkeypatch):
    _install_real_runtime_mocks(monkeypatch)
    bench = NetOpsBench(workspace=str(tmp_path))
    runtime = bench.runtimes.create(scale="xs", workers=1, name="trace-runtime")
    scenario = _make_scenario(scenario_id="trace-scenario")

    class TraceAgent:
        name = "trace-agent"

        def diagnose(self, context):
            context.trace.record_llm_request([{"role": "user", "content": "diagnose"}], run_id="llm-1")
            context.trace.record_llm_response(
                SimpleNamespace(
                    generations=[[SimpleNamespace(message=SimpleNamespace(type="ai", content="checking"))]]
                ),
                run_id="llm-1",
            )
            return DiagnosisResult(
                agent_name="trace-agent",
                verdict="inconclusive",
                findings={"fault_type": None, "location": {}, "evidence": []},
                confidence=0.0,
                reasoning="trace captured",
                metadata={},
            )

    run = bench.sessions.run_on_runtime_scenario(
        scenario=scenario,
        runtime=runtime,
        agent=TraceAgent(),
        artifacts_dir=tmp_path / "trace-runs",
    )

    report = run.report()
    raw_result_path = Path(report.scenario_summaries[0]["raw_result_path"])
    raw_result = json.loads(raw_result_path.read_text(encoding="utf-8"))
    diagnosis = raw_result["episodes"][0]["diagnosis"]

    assert "trace" not in diagnosis["metadata"]
    assert "trajectory" not in diagnosis["metadata"]
    assert diagnosis["trace"]["trace_id"]
    assert Path(diagnosis["trace"]["atif_path"]).exists()


def test_runtime_agent_failure_trace_is_linked_from_results_sidecar(tmp_path, monkeypatch):
    _install_real_runtime_mocks(monkeypatch)

    import netopsbench.platform.session.orchestrator as sessions_mod

    class LinkedEvalResult:
        score = 0.0

        def to_dict(self):
            return {
                "testcase_id": "failure-scenario:ep1",
                "score": 0.0,
                "details": {"scenario_id": "failure-scenario", "episode_id": "ep1"},
            }

    monkeypatch.setattr(sessions_mod, "score_scenario_fault_episodes", lambda *args, **kwargs: [LinkedEvalResult()])

    bench = NetOpsBench(workspace=str(tmp_path))
    runtime = bench.runtimes.create(scale="xs", workers=1, name="trace-failure-runtime")
    scenario = _make_scenario(scenario_id="failure-scenario")

    class FailingAgent:
        name = "failing-agent"
        vendor = "deepseek"
        model = "deepseek-v4-pro"

        def diagnose(self, context):
            raise ValueError("agent exploded")

    run = bench.sessions.run_on_runtime_scenario(
        scenario=scenario,
        runtime=runtime,
        agent=FailingAgent(),
        artifacts_dir=tmp_path / "trace-failure-runs",
    )

    report = run.report()
    raw_result_path = Path(report.scenario_summaries[0]["raw_result_path"])
    raw_result = json.loads(raw_result_path.read_text(encoding="utf-8"))
    diagnosis = raw_result["episodes"][0]["diagnosis"]

    assert diagnosis["success"] is False
    assert diagnosis["error"] == "agent exploded"
    assert diagnosis["trace"]["trace_id"]
    assert diagnosis["trace"]["case_id"].startswith("case-")

    result_rows = [
        json.loads(line)
        for line in (Path(run.artifact_dir) / "traces" / "results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert result_rows[0]["trace_id"] == diagnosis["trace"]["trace_id"]
    assert result_rows[0]["case_id"] == diagnosis["trace"]["case_id"]
    assert result_rows[0]["case_id"] is not None

    index_rows = [
        json.loads(line)
        for line in (Path(run.artifact_dir) / "traces" / "index.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert index_rows[0]["provider"] == "deepseek"
    assert index_rows[0]["model"] == "deepseek-v4-pro"
    assert index_rows[0]["topology_scale"] == "xs"


def test_runtime_trace_false_disables_trace_artifacts_and_recorder_capture(tmp_path, monkeypatch):
    _install_real_runtime_mocks(monkeypatch)
    bench = NetOpsBench(workspace=str(tmp_path))
    runtime = bench.runtimes.create(scale="xs", workers=1, name="trace-off-runtime")
    scenario = _make_scenario(scenario_id="trace-off-scenario")

    class TraceOffAgent:
        name = "trace-off-agent"

        def __init__(self):
            self.trace_enabled = None

        def diagnose(self, context):
            self.trace_enabled = getattr(context.trace, "enabled", None)
            context.trace.record_llm_request([{"role": "user", "content": "private prompt"}], run_id="llm-1")
            context.trace.record_llm_response(
                SimpleNamespace(
                    generations=[
                        [
                            SimpleNamespace(
                                message=SimpleNamespace(
                                    type="ai",
                                    content="private response",
                                    usage_metadata={"input_tokens": 99, "output_tokens": 1, "total_tokens": 100},
                                )
                            )
                        ]
                    ]
                ),
                run_id="llm-1",
            )
            return DiagnosisResult(
                agent_name="trace-off-agent",
                verdict="inconclusive",
                findings={"fault_type": None, "location": {}, "evidence": []},
                confidence=0.0,
                reasoning="trace disabled",
                metadata={"input_tokens": 3, "tool_calls": [{"tool": "manual"}]},
            )

    agent = TraceOffAgent()
    run = bench.sessions.run_on_runtime_scenario(
        scenario=scenario,
        runtime=runtime,
        agent=agent,
        artifacts_dir=tmp_path / "trace-off-runs",
        trace=False,
    )

    report = run.report()
    raw_result_path = Path(report.scenario_summaries[0]["raw_result_path"])
    raw_result = json.loads(raw_result_path.read_text(encoding="utf-8"))
    diagnosis = raw_result["episodes"][0]["diagnosis"]

    assert agent.trace_enabled is False
    assert "trace" not in diagnosis
    assert diagnosis["metadata"]["input_tokens"] == 3
    assert diagnosis["tool_calls"] == [{"tool": "manual"}]
    assert "traces_dir" not in report.artifact_paths
    assert not (Path(run.artifact_dir) / "traces").exists()


def test_runtime_session_does_not_override_process_env_during_diagnosis(tmp_path, monkeypatch):
    _install_real_runtime_mocks(monkeypatch)
    bench = NetOpsBench(workspace=str(tmp_path))
    runtime = bench.runtimes.create(scale="xs", workers=1, name="env-runtime")
    scenario = _make_scenario(scenario_id="env-scenario")

    monkeypatch.setenv("NETOPSBENCH_TOPOLOGY_DIR", "outer-topology-dir")
    monkeypatch.setenv("NETOPSBENCH_TOPOLOGY_ID", "outer-topology-id")
    monkeypatch.setenv("NETOPSBENCH_INFLUXDB_BUCKET", "outer-bucket")

    class EnvCaptureAgent:
        def __init__(self):
            self.captured = None

        def diagnose(self, context):
            self.captured = {
                "topology_dir": os.environ.get("NETOPSBENCH_TOPOLOGY_DIR"),
                "topology_id": os.environ.get("NETOPSBENCH_TOPOLOGY_ID"),
                "bucket": os.environ.get("NETOPSBENCH_INFLUXDB_BUCKET"),
            }
            return DiagnosisResult(
                agent_name="env-capture-agent",
                verdict="inconclusive",
                findings={"evidence": ["env captured"]},
                confidence=0.1,
                reasoning="captured env state",
                metadata={},
            )

    agent = EnvCaptureAgent()
    run = bench.sessions.run_on_runtime_scenario(
        scenario=scenario,
        runtime=runtime,
        agent=agent,
        artifacts_dir=tmp_path / "env-runs",
    )

    assert run.status == "completed"
    assert agent.captured == {
        "topology_dir": "outer-topology-dir",
        "topology_id": "outer-topology-id",
        "bucket": "outer-bucket",
    }


def test_session_manager_signatures_match_public_surface_without_provider_or_model():
    sessions = NetOpsBench().sessions
    methods = {
        "run_scenario": inspect.signature(sessions.run_scenario),
        "run_suite": inspect.signature(sessions.run_suite),
        "run_on_runtime_scenario": inspect.signature(sessions.run_on_runtime_scenario),
        "run_on_runtime_suite": inspect.signature(sessions.run_on_runtime_suite),
    }

    assert list(methods["run_scenario"].parameters) == [
        "scenario",
        "agent",
        "scale",
        "workers",
        "root_dir",
        "keep_runtime",
        "artifacts_dir",
        "trace",
    ]
    assert list(methods["run_suite"].parameters) == [
        "scenarios",
        "agent",
        "scale",
        "workers",
        "root_dir",
        "keep_runtime",
        "artifacts_dir",
        "trace",
    ]
    assert list(methods["run_on_runtime_scenario"].parameters) == [
        "scenario",
        "runtime",
        "agent",
        "artifacts_dir",
        "trace",
    ]
    assert list(methods["run_on_runtime_suite"].parameters) == [
        "scenarios",
        "runtime",
        "agent",
        "artifacts_dir",
        "trace",
    ]

    for sig in methods.values():
        assert sig.parameters["trace"].default is True
        for forbidden in ("provider", "model", "model_name"):
            assert forbidden not in sig.parameters


def test_run_handle_exposes_report_wait_refresh_and_cancel_contract(tmp_path):
    report = BenchmarkReport(
        id="run:run-0009",
        summary={"status": "completed", "mode": "scenario"},
        scenario_summaries=[{"scenario_id": "s-1"}],
        detailed_results=[],
        artifact_paths={"report": str(tmp_path / "report.json")},
        raw={"status": "completed", "runtime_id": "rt-9"},
    )
    assert report.save(tmp_path / "report.json") is None
    run = RunHandle(
        id="run-0009",
        mode="scenario",
        status="running",
        started_at=datetime.fromisoformat("2026-04-01T00:00:00+00:00"),
        completed_at=None,
        artifact_dir=str(tmp_path),
        scenario_ids=["s-1"],
        runtime_id="rt-9",
        report_path=tmp_path / "report.json",
    )

    assert run.report().id == "run:run-0009"
    waited = run.wait()
    assert waited.id == "run:run-0009"
    refreshed = run.refresh()
    assert refreshed.status == "completed"
    assert isinstance(refreshed.completed_at, datetime)
    assert refreshed.cancel() is None
    assert refreshed.status == "completed"

    pending = RunHandle(
        id="run-0010",
        mode="suite",
        status="running",
        started_at=datetime.fromisoformat("2026-04-01T00:00:00+00:00"),
        completed_at=None,
        artifact_dir=str(tmp_path / "missing"),
        scenario_ids=["s-a", "s-b"],
        runtime_id="rt-10",
        report_path=tmp_path / "missing" / "report.json",
    )
    assert pending.report() is None
    assert pending.cancel() is None
    assert pending.status == "cancelled"
    assert isinstance(pending.completed_at, datetime)


def test_keep_runtime_false_tears_down_runtime_and_true_preserves_it(tmp_path, monkeypatch):
    _install_real_runtime_mocks(monkeypatch)
    _install_platform_runtime_mocks(monkeypatch)
    bench = NetOpsBench(workspace=str(tmp_path))
    scenario = _make_scenario(scenario_id="scenario-keep")

    removed = bench.sessions.run_scenario(scenario=scenario, agent=DummyAgent(), keep_runtime=False)
    preserved = bench.sessions.run_scenario(scenario=scenario, agent=DummyAgent(), keep_runtime=True)

    assert bench.runtimes.get(removed.runtime_id) is None
    assert not (tmp_path / ".netopsbench" / "runtimes" / removed.runtime_id).exists()
    assert bench.runtimes.get(preserved.runtime_id) is not None
    assert (tmp_path / ".netopsbench" / "runtimes" / preserved.runtime_id).exists()
    assert preserved.report().raw["runtime_owner"] == "platform"
    assert preserved.report().raw["execution"] == "real_runtime_runner"


def test_benchmark_report_exposes_spec_shape_and_void_helpers(tmp_path, capsys):
    report = BenchmarkReport(
        id="report-123",
        summary={"total_cases": 2, "average_score": 0.0},
        scenario_summaries=[{"scenario_id": "s-1"}, {"scenario_id": "s-2"}],
        detailed_results=[{"scenario_id": "s-1", "status": "stubbed"}],
        artifact_paths={"report": str(tmp_path / "report.json")},
        raw={"status": "completed", "scaffold": True},
    )

    assert report.to_dict()["id"] == "report-123"
    assert '"report-123"' in report.to_json()
    assert report.save(tmp_path / "report.json") is None
    assert report.pretty_print() is None
    out = capsys.readouterr().out
    # Default pretty_print renders a human-readable report with the per-case
    # scenario id rather than a raw JSON dump.
    assert "s-1" in out
    assert report.pretty_print(json=True) is None
    assert "scenario_summaries" in capsys.readouterr().out

    loaded = BenchmarkReport.load(tmp_path / "report.json")
    assert loaded.id == "report-123"
    assert loaded.raw["status"] == "completed"


def test_run_scenario_does_not_leak_fault_ground_truth_into_agent_context(tmp_path, monkeypatch):
    _install_real_runtime_mocks(monkeypatch)
    _install_platform_runtime_mocks(monkeypatch)
    bench = NetOpsBench(workspace=str(tmp_path))
    scenario = _make_scenario(scenario_id="scenario-sanitized")
    agent = RecordingAgent()

    run = bench.sessions.run_scenario(
        scenario=scenario,
        agent=agent,
        scale="xs",
        workers=1,
        keep_runtime=True,
    )

    assert run.status == "completed"
    assert len(agent.contexts) == 1
    context = agent.contexts[0]
    episode = dict(context.symptoms.get("episode") or {})
    assert episode.get("episode_id") == "ep1"
    assert "description" not in episode
    assert "fault_type" not in episode
    assert "target_device" not in episode
    assert "target_interface" not in episode
    assert context.ground_truth is None
