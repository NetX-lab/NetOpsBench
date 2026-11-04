"""Behavior preserved when internal representations and cleanup are consolidated."""

import pytest
from pydantic import ValidationError

from netopsbench.agents.base import DiagnosisResult
from netopsbench.evaluator.scorer import AgentOutput
from netopsbench.platform.incident.engine import SessionState, SessionTransition
from netopsbench.platform.scenario.executor import ScenarioExecutor
from netopsbench.platform.simulator.environment import StepResult
from netopsbench.platform.topology.generator import generate_topology


def test_case_reset_preserves_runtime_state_and_clears_all_case_evidence(tmp_path):
    generate_topology("xs", str(tmp_path))
    runner = ScenarioExecutor(topology_dir=str(tmp_path))
    traffic = runner.traffic_controller = object()
    handoff = runner._baseline_handoff
    runner._healthy_baseline_candidate = {"old": True}
    runner._case_frr_before = {"old": True}
    runner._case_initial_scope = object()
    runner._case_routes = {"old": True}
    runner._case_affected_pairs = ["old"]
    runner._case_probe_pairs = ["old"]
    runner.timings.skip("injection", "old")
    runner.begin_case()
    assert runner.traffic_controller is traffic
    assert runner._baseline_handoff is handoff
    assert runner.timings.phases == {}
    assert runner._healthy_baseline_candidate is runner._case_frr_before is None
    assert runner._case_initial_scope is runner._case_routes is None
    assert runner._case_affected_pairs == runner._case_probe_pairs == []


def test_public_step_required_flags_and_transition_serialization_are_preserved():
    transition = SessionTransition(state=SessionState.ACTIVE)
    with pytest.raises(ValidationError):
        StepResult(state=SessionState.ACTIVE)
    result = StepResult.model_validate(transition.model_dump())
    assert result.model_dump() == transition.model_dump()
    assert set(StepResult.model_json_schema()["required"]) == {"valid", "case_valid", "state"}
    with pytest.raises(ValidationError):
        result.valid = False


def test_diagnosis_entrypoint_precedence_is_preserved():
    diagnosis = DiagnosisResult(
        agent_name="custom",
        verdict="fault_detected",
        findings={
            "location": {"device": "leaf1"},
            "device": "leaf2",
            "interface": "Ethernet0",
            "tool_calls": [{"tool": "findings"}],
        },
        metadata={"evidence": ["metadata evidence"], "fault_type": "link_down", "time_taken_seconds": 9},
    )
    sdk = AgentOutput.from_diagnosis(diagnosis)
    runtime = AgentOutput.from_diagnosis(
        diagnosis,
        merge_location_fields=True,
        metadata_evidence=False,
        findings_tool_calls=False,
        time_taken_seconds=2,
    )
    assert sdk.location == {"device": "leaf1"}
    assert runtime.location == {"device": "leaf1", "interface": "Ethernet0"}
    assert sdk.evidence == ["metadata evidence"] and runtime.evidence == []
    assert sdk.tool_calls == [{"tool": "findings"}] and runtime.tool_calls == []
    assert sdk.time_taken_seconds == 9 and runtime.time_taken_seconds == 2
    assert sdk.fault_type == runtime.fault_type == "link_down"
    assert diagnosis.findings["location"] == {"device": "leaf1"}


def test_teardown_interrupted_deployment_preserves_cleanup_error_when_metadata_fails(tmp_path, monkeypatch):
    from netopsbench.platform.runtime import manager as module

    runtime = module.RuntimeManager(workspace=str(tmp_path)).create(scale="xs", name="interrupted")
    runtime.metadata["deployment_started"] = True
    failure = KeyboardInterrupt("cleanup interrupted")
    calls = []

    def interrupt(self, stage, pool):
        calls.append(stage)
        assert pool is runtime
        raise failure

    def persist_failure():
        raise OSError("metadata unavailable")

    monkeypatch.setattr(module.RuntimeLifecycle, "run", interrupt)
    monkeypatch.setattr(runtime, "_write_metadata", persist_failure)
    with pytest.raises(KeyboardInterrupt) as caught:
        runtime.teardown()
    assert caught.value is failure
    assert calls == ["teardown"]
    assert runtime.state == "cleanup_failed"
    assert runtime.metadata["quarantined"] is True
    assert "metadata unavailable" in runtime.metadata["quarantine_persistence_error"]
    assert runtime.root_dir.exists()


@pytest.mark.parametrize("text,expected", [("1G", 1e9), (" 2.5m ", 2.5e6), ("12.9", 12.9), ("0", 0)])
def test_bandwidth_estimation_and_agent_plan_share_units(text, expected):
    from netopsbench.platform.traffic.controller import TrafficFlow, _client_plans
    from netopsbench.platform.traffic.estimator import bandwidth_to_bps

    assert bandwidth_to_bps(text) == expected
    flow = TrafficFlow(src="a", dst="b", dst_ip="192.0.2.2", bandwidth=text)
    plans = _client_plans({"a": "192.0.2.1", "b": "192.0.2.2"}, [flow])
    assert plans["a"]["flows"][0]["bandwidth_bps"] == int(expected)
