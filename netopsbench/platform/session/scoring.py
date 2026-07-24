"""Scenario scoring helpers."""

from typing import Any

from netopsbench.evaluator.scorer import AgentOutput, EvaluationResult, Evaluator
from netopsbench.platform.incident.scoring import build_episode_ground_truth


def diagnosis_to_agent_output(diagnosis: dict[str, Any] | None) -> AgentOutput:
    if not diagnosis or diagnosis.get("error"):
        error = diagnosis.get("error") if isinstance(diagnosis, dict) else "diagnosis_missing"
        return AgentOutput(
            verdict="inconclusive",
            fault_type=None,
            location={},
            evidence=[f"diagnosis_unavailable: {error}"],
            confidence=0.0,
            reasoning="No valid diagnosis available for this fault episode.",
            tool_calls=[],
            time_taken_seconds=0.0,
            metadata={"final_status": "diagnosis_unavailable", "error": error},
        )
    return AgentOutput(
        verdict=diagnosis.get("verdict", "network_healthy"),
        fault_type=diagnosis.get("fault_type"),
        location=diagnosis.get("location") or {},
        evidence=diagnosis.get("evidence") or [],
        confidence=float(diagnosis.get("confidence", 0.0) or 0.0),
        reasoning=diagnosis.get("reasoning", ""),
        tool_calls=diagnosis.get("tool_calls") or [],
        time_taken_seconds=float(diagnosis.get("time_taken_seconds", 0.0) or 0.0),
        metadata=diagnosis.get("metadata") or {},
    )


def score_scenario_episode(
    scenario,
    scenario_result: dict[str, Any],
    evaluator: Evaluator,
    topology_dir: str | None = None,
) -> list[EvaluationResult]:
    scored_results: list[EvaluationResult] = []
    scenario_difficulty = (scenario.metadata or {}).get("difficulty", "unknown")
    episode_result = scenario_result.get("episode") or {}
    episode_info = episode_result.get("episode", {})
    if not episode_info:
        return scored_results
    testcase_id = f"{scenario.scenario_id}:{episode_info.get('episode_id', 'unknown')}"
    ground_truth = (
        {} if episode_info.get("fault_type") == "none" else build_episode_ground_truth(episode_info, topology_dir)
    )
    persisted_evaluation = episode_result.get("evaluation_result")
    if isinstance(persisted_evaluation, dict):
        eval_result = EvaluationResult(**persisted_evaluation)
    else:
        agent_output = diagnosis_to_agent_output(episode_result.get("diagnosis"))
        eval_result = evaluator.evaluate(agent_output, ground_truth, testcase_id)
    eval_result.details["difficulty"] = scenario_difficulty
    eval_result.details["scenario_id"] = scenario.scenario_id
    eval_result.details["episode_id"] = episode_info.get("episode_id")
    eval_result.details["healthy"] = episode_info.get("fault_type") == "none"
    scored_results.append(eval_result)
    return scored_results
