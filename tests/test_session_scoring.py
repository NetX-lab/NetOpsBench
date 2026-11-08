"""Persisted incident grades skip independent ground-truth reconstruction."""

from types import SimpleNamespace

from netopsbench.evaluator.scorer import EvaluationResult
from netopsbench.platform.session import scoring


def test_persisted_grade_does_not_rebuild_truth_or_call_evaluator(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Persisted scores must not query topology or evaluate again")

    monkeypatch.setattr(scoring, "build_episode_ground_truth", unexpected)
    grade = EvaluationResult("opaque-incident", True, True, False, True, 0.5)
    result = scoring.score_scenario_episode(
        SimpleNamespace(scenario_id="scenario", metadata={}),
        {
            "episode": {
                "episode": {"episode_id": "case", "fault_type": "link_down"},
                "evaluation_result": grade.to_dict(),
            }
        },
        SimpleNamespace(evaluate=unexpected),
    )
    assert len(result) == 1 and result[0].score == 0.5
    assert result[0].testcase_id == "opaque-incident"
    assert result[0].details["episode_id"] == "case"


def test_independent_evaluation_still_builds_truth_when_grade_is_absent(monkeypatch):
    truth = {"device": "leaf1"}
    calls = []
    monkeypatch.setattr(
        scoring, "build_episode_ground_truth", lambda episode, directory: calls.append(directory) or truth
    )

    def evaluate(agent, ground_truth, testcase_id):
        assert ground_truth is truth
        assert agent.verdict == "inconclusive"
        return EvaluationResult(testcase_id, False, False, False, False, 0)

    result = scoring.score_scenario_episode(
        SimpleNamespace(scenario_id="scenario", metadata={}),
        {"episode": {"episode": {"episode_id": "case", "fault_type": "link_down"}}},
        SimpleNamespace(evaluate=evaluate),
        topology_dir="topology",
    )
    assert calls == ["topology"]
    assert result[0].score == 0


def test_diagnosis_entrypoint_precedence_is_preserved():
    from netopsbench.agents.base import DiagnosisResult
    from netopsbench.evaluator.scorer import AgentOutput

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
