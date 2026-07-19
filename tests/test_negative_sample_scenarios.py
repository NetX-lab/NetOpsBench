"""Healthy scenarios use the same canonical episode path as fault cases."""

from __future__ import annotations

import tempfile

from netopsbench.evaluator.scorer import AgentOutput, Evaluator
from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.platform.scenario.executor import ScenarioExecutor
from netopsbench.platform.session.scoring import score_scenario_episode
from netopsbench.platform.topology.generator import generate_topology


def _metadata() -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        return generate_topology("xs", tmpdir)["metadata"]


def _healthy_scenario() -> ScenarioSpec:
    return ScenarioSpec(
        scenario_id="healthy_001",
        name="healthy",
        topology_scale="xs",
        episode=EpisodeSpec(
            episode_id="diagnosis",
            description="healthy observation",
            fault_type="none",
            duration_seconds=20,
            stabilization_time=5,
        ),
    )


def test_healthy_scenario_observes_and_diagnoses(monkeypatch):
    runner = ScenarioExecutor(
        topology_metadata=_metadata(),
        sleep_fn=lambda _seconds: None,
        persist_results=False,
    )
    monkeypatch.setattr(runner, "_setup_traffic", lambda scale, profile: {"ok": True})
    monkeypatch.setattr(runner, "_stop_traffic", lambda: None)
    monkeypatch.setattr(runner, "_recover_fault", lambda: [])
    monkeypatch.setattr(
        runner,
        "_wait_and_observe",
        lambda duration, **_kwargs: {
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T00:01:00Z",
            "duration_seconds": duration,
            "pingmesh_metrics": {"summary": {"total_anomalies": 0}, "anomalies": []},
            "anomalies_detected": False,
            "data_source_status": "ok",
        },
    )
    calls = []

    result = runner.run_scenario(
        _healthy_scenario(),
        diagnosis_callback=lambda payload: calls.append(payload) or {"verdict": "network_healthy"},
    )

    assert result["success"] is True
    assert len(calls) == 1
    assert result["episode"]["observations"]["data_source_status"] == "ok"
    assert result["episode"]["diagnosis"]["verdict"] == "network_healthy"


def test_agent_exception_is_zero_outcome_not_infrastructure_failure(monkeypatch):
    runner = ScenarioExecutor(
        topology_metadata=_metadata(),
        sleep_fn=lambda _seconds: None,
        persist_results=False,
    )
    monkeypatch.setattr(runner, "_setup_traffic", lambda scale, profile: {"ok": True})
    monkeypatch.setattr(runner, "_stop_traffic", lambda: None)
    monkeypatch.setattr(runner, "_recover_fault", lambda: [])
    monkeypatch.setattr(
        runner,
        "_wait_and_observe",
        lambda duration, **_kwargs: {
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T00:01:00Z",
            "duration_seconds": duration,
            "pingmesh_metrics": {"summary": {"total_anomalies": 0}, "anomalies": []},
            "anomalies_detected": False,
            "data_source_status": "ok",
        },
    )

    def fail(_payload):
        raise RuntimeError("agent crashed")

    result = runner.run_scenario(_healthy_scenario(), diagnosis_callback=fail)

    assert result["success"] is True
    assert result["episode"]["diagnosis"]["error"] == "agent crashed"
    assert result["episode"]["execution"]["reward"] == 0.0
    assert result["episode"]["execution"]["failure"]["domain"] == "agent"


def test_healthy_scenario_scores_network_healthy_as_correct():
    result = {
        "episode": {
            "episode": {"episode_id": "diagnosis", "fault_type": "none"},
            "diagnosis": {"verdict": "network_healthy", "confidence": 1.0},
        }
    }

    scored = score_scenario_episode(_healthy_scenario(), result, Evaluator())

    assert len(scored) == 1
    assert scored[0].correct_verdict is True
    assert scored[0].score == 1.0
    assert scored[0].details["healthy"] is True


def test_healthy_scenario_scores_fault_detected_as_false_positive():
    result = {
        "episode": {
            "episode": {"episode_id": "diagnosis", "fault_type": "none"},
            "diagnosis": {"verdict": "fault_detected", "fault_type": "link_down"},
        }
    }

    scored = score_scenario_episode(_healthy_scenario(), result, Evaluator())

    assert scored[0].correct_verdict is False
    assert scored[0].score == 0.0
    assert scored[0].details["false_positive"] is True


def test_report_localization_ignores_healthy_samples():
    evaluator = Evaluator()
    positive = evaluator.evaluate(
        AgentOutput(verdict="fault_detected", fault_type="link_down", location={"device": "wrong"}),
        {"fault_type": "link_down", "location": {"device": "leaf1"}},
        "fault",
    )
    healthy = evaluator.evaluate(AgentOutput(verdict="network_healthy"), {}, "healthy")

    report = evaluator.generate_report([positive, healthy])

    assert report["summary"]["positive_sample_cases"] == 1
    assert report["summary"]["negative_sample_cases"] == 1
    assert report["summary"]["correct_device"] == 0
    assert report["summary"]["device_localization_rate"] == 0.0


def test_negative_sample_preserves_agent_accounting_in_report():
    evaluator = Evaluator()
    healthy = evaluator.evaluate(
        AgentOutput(
            verdict="network_healthy",
            confidence=0.91,
            tool_calls=[{"tool": "get_pingmesh_summary"}, {"tool": "query_bgp_events"}],
            time_taken_seconds=12.5,
            metadata={"input_tokens": 1200, "output_tokens": 80},
        ),
        {},
        "healthy",
    )

    assert healthy.details["agent_output"]["verdict"] == "network_healthy"
    assert healthy.details["tool_calls_count"] == 2
    assert healthy.details["time_taken"] == 12.5
    assert healthy.details["confidence"] == 0.91
    assert healthy.details["inconclusive"] is False

    report = evaluator.generate_report([healthy])
    assert report["summary"]["avg_tool_calls"] == 2.0
    assert report["summary"]["avg_time_seconds"] == 12.5
    assert report["summary"]["total_input_tokens"] == 1200
    assert report["summary"]["total_output_tokens"] == 80
