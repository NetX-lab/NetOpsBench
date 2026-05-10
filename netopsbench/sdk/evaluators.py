"""Public evaluator adapters for the NetOpsBench SDK."""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Mapping
from typing import Any

from netopsbench.evaluator.fault_type_judge import FaultTypeJudge, create_judge_from_env
from netopsbench.evaluator.scorer import AgentOutput, create_default_evaluator, create_fault_type_judge_evaluator
from netopsbench.sdk.reports import BenchmarkReport
from netopsbench.sdk.scenarios import ScenarioHandle
from netopsbench.sdk.types import DiagnosisResult, DiagnosticContext


class _DefaultEvaluatorAdapter:
    def __init__(self, *, name: str = "default", scorer: Any | None = None):
        self.name = name
        if scorer is not None:
            self._scorer = scorer
        else:
            judge = create_judge_from_env()
            if judge is not None:
                self._scorer = create_fault_type_judge_evaluator(judge)
            else:
                self._scorer = create_default_evaluator()

    def evaluate_scenario(
        self, *, scenario: ScenarioHandle, diagnosis_results: list[DiagnosisResult], evaluator: str = "default"
    ) -> BenchmarkReport:
        scored_results = self._score_scenario(scenario, diagnosis_results)
        payload = self._build_payload(
            scored_results, agent_name=_agent_name(diagnosis_results), topology_scale=scenario.scale
        )
        payload.update({"evaluator": evaluator, "scope": "scenario", "scenario_id": scenario.id})
        return BenchmarkReport(report_id=f"scenario:{scenario.id}", payload=payload)

    def evaluate_suite(
        self, *, scenarios: list[ScenarioHandle], diagnosis_results: list[DiagnosisResult], evaluator: str = "default"
    ) -> BenchmarkReport:
        if len(scenarios) != len(diagnosis_results):
            raise ValueError("diagnosis_results must align 1:1 with scenarios for suite evaluation")
        scored_results: list[Any] = []
        for scenario, diagnosis in zip(scenarios, diagnosis_results, strict=False):
            scored_results.extend(self._score_scenario(scenario, [diagnosis]))
        payload = self._build_payload(
            scored_results, agent_name=_agent_name(diagnosis_results), topology_scale=_suite_scale(scenarios)
        )
        payload.update(
            {"evaluator": evaluator, "scope": "suite", "scenario_ids": [scenario.id for scenario in scenarios]}
        )
        return BenchmarkReport(report_id=f"suite:{len(scenarios)}", payload=payload)

    def _score_scenario(self, scenario: ScenarioHandle, diagnosis_results: list[DiagnosisResult]) -> list[Any]:
        scored_results = []
        for index, diagnosis in enumerate(diagnosis_results):
            result = self._scorer.evaluate(
                agent_output=_diagnosis_to_agent_output(diagnosis),
                ground_truth=_build_ground_truth(scenario, index),
                testcase_id=f"{scenario.id}:{index}",
            )
            result.details["difficulty"] = scenario.metadata.get("difficulty", "unknown")
            scored_results.append(result)
        return scored_results

    def _build_payload(self, scored_results: list[Any], *, agent_name: str, topology_scale: str) -> dict[str, Any]:
        if not scored_results:
            return {
                "agent_name": agent_name,
                "topology_scale": topology_scale,
                "summary": {"total_cases": 0, "average_score": 0.0},
                "detailed_results": [],
            }
        return self._scorer.generate_report(scored_results, agent_name=agent_name, topology_scale=topology_scale)


class _WrappedPublicEvaluator:
    def __init__(self, evaluator: Any, *, name: str):
        self._evaluator = evaluator
        self.name = name

    def evaluate_scenario(
        self, *, scenario: ScenarioHandle, diagnosis_results: list[DiagnosisResult], evaluator: str = "default"
    ) -> BenchmarkReport:
        if hasattr(self._evaluator, "evaluate_scenario"):
            report = self._evaluator.evaluate_scenario(
                scenario=scenario, diagnosis_results=list(diagnosis_results), evaluator=evaluator
            )
            return _coerce_report(report, report_id=f"scenario:{scenario.id}", evaluator_name=evaluator)
        if hasattr(self._evaluator, "evaluate"):
            results: list[dict[str, Any]] = []
            for index, diagnosis in enumerate(diagnosis_results):
                context = DiagnosticContext(
                    scenario_id=scenario.id,
                    topology={"scale": scenario.scale},
                    symptoms={},
                    ground_truth=_build_ground_truth(scenario, index),
                    metadata={"scenario": scenario.to_dict(), "episode_index": index},
                    tools=None,
                )
                results.append(dict(self._evaluator.evaluate(context, diagnosis)))
            return _report_from_result_items(
                report_id=f"scenario:{scenario.id}", evaluator_name=evaluator, results=results
            )
        raise TypeError(f"Evaluator '{self.name}' does not provide evaluate_scenario() or evaluate()")

    def evaluate_suite(
        self, *, scenarios: list[ScenarioHandle], diagnosis_results: list[DiagnosisResult], evaluator: str = "default"
    ) -> BenchmarkReport:
        if hasattr(self._evaluator, "evaluate_suite"):
            report = self._evaluator.evaluate_suite(
                scenarios=list(scenarios), diagnosis_results=list(diagnosis_results), evaluator=evaluator
            )
            return _coerce_report(report, report_id=f"suite:{len(scenarios)}", evaluator_name=evaluator)
        if len(scenarios) != len(diagnosis_results):
            raise ValueError("diagnosis_results must align 1:1 with scenarios for suite evaluation")
        results: list[dict[str, Any]] = []
        for scenario, diagnosis in zip(scenarios, diagnosis_results, strict=False):
            scenario_report = self.evaluate_scenario(
                scenario=scenario, diagnosis_results=[diagnosis], evaluator=evaluator
            )
            results.extend(_extract_report_results(scenario_report))
        return _report_from_result_items(report_id=f"suite:{len(scenarios)}", evaluator_name=evaluator, results=results)


class EvaluatorManager:
    """Thin public registry for scenario evaluators."""

    def __init__(self):
        default = _DefaultEvaluatorAdapter(name="default")
        self._evaluators: dict[str, Any] = {"default": default}

    def register(self, name: str, evaluator: Any) -> Any:
        evaluator_name = str(name or "").strip()
        if not evaluator_name:
            raise ValueError("evaluator name must be a non-empty string")
        normalized = _normalize_evaluator(evaluator, name=evaluator_name)
        self._evaluators[evaluator_name] = normalized
        return normalized

    def get(self, name: str) -> Any:
        evaluator_name = str(name or "").strip()
        evaluator = self._evaluators.get(evaluator_name)
        if evaluator is None:
            raise KeyError(f"Evaluator '{evaluator_name}' not found")
        return evaluator

    def list(self) -> builtins.list[str]:
        return sorted(self._evaluators)

    def evaluate_scenario(
        self, *, scenario: ScenarioHandle, diagnosis_results: builtins.list[DiagnosisResult], evaluator: str = "default"
    ) -> BenchmarkReport:
        adapter = self.get(evaluator)
        report = adapter.evaluate_scenario(
            scenario=scenario, diagnosis_results=list(diagnosis_results), evaluator=evaluator
        )
        return _coerce_report(report, report_id=f"scenario:{scenario.id}", evaluator_name=evaluator)

    def evaluate_suite(
        self,
        *,
        scenarios: builtins.list[ScenarioHandle],
        diagnosis_results: builtins.list[DiagnosisResult],
        evaluator: str = "default",
    ) -> BenchmarkReport:
        scenario_list = list(scenarios)
        diagnosis_list = list(diagnosis_results)
        adapter = self.get(evaluator)
        report = adapter.evaluate_suite(scenarios=scenario_list, diagnosis_results=diagnosis_list, evaluator=evaluator)
        return _coerce_report(report, report_id=f"suite:{len(scenario_list)}", evaluator_name=evaluator)


def create_fault_type_judge_evaluator_adapter(
    fault_type_judge: FaultTypeJudge, *, name: str = "llm-fault-type-v1"
) -> Any:
    """Create an SDK evaluator adapter that judges fault types semantically.

    Register the returned adapter with :class:`EvaluatorManager` under a versioned
    evaluator name, for example ``manager.register("llm-fault-type-v1", adapter)``.
    """
    return _DefaultEvaluatorAdapter(name=name, scorer=create_fault_type_judge_evaluator(fault_type_judge))


def _normalize_evaluator(evaluator: Any, *, name: str) -> Any:
    if (
        hasattr(evaluator, "evaluate_scenario")
        or hasattr(evaluator, "evaluate_suite")
        or hasattr(evaluator, "evaluate")
    ):
        if isinstance(evaluator, (_DefaultEvaluatorAdapter, _WrappedPublicEvaluator)):
            return evaluator
        return _WrappedPublicEvaluator(evaluator, name=name)
    raise TypeError("evaluator must provide evaluate_scenario(), evaluate_suite(), or evaluate()")


def _coerce_report(report: Any, *, report_id: str, evaluator_name: str) -> BenchmarkReport:
    if isinstance(report, BenchmarkReport):
        return report
    if isinstance(report, Mapping):
        return _report_from_result_items(report_id=report_id, evaluator_name=evaluator_name, results=[dict(report)])
    raise TypeError("evaluator output must be a BenchmarkReport or mapping")


def _report_from_result_items(*, report_id: str, evaluator_name: str, results: list[dict[str, Any]]) -> BenchmarkReport:
    scores = [float(item.get("score", 0.0)) for item in results]
    average_score = round(sum(scores) / len(scores), 3) if scores else 0.0
    return BenchmarkReport(
        report_id=report_id,
        payload={
            "evaluator": evaluator_name,
            "summary": {"total_cases": len(results), "average_score": average_score},
            "results": results,
        },
    )


def _extract_report_results(report: BenchmarkReport) -> list[dict[str, Any]]:
    payload = dict(report.payload)
    result_items = payload.get("results")
    if isinstance(result_items, list):
        return [dict(item) for item in result_items]
    detailed_items = payload.get("detailed_results")
    if isinstance(detailed_items, list):
        return [dict(item) for item in detailed_items]
    return []


def _diagnosis_to_agent_output(diagnosis: DiagnosisResult) -> AgentOutput:
    findings = dict(diagnosis.findings or {})
    metadata = dict(diagnosis.metadata or {})
    location = findings.get("location") if isinstance(findings.get("location"), dict) else {}
    if not location:
        location = {
            key: findings[key] for key in ("device", "interface") if key in findings and findings[key] is not None
        }
    return AgentOutput(
        verdict=diagnosis.verdict,
        fault_type=findings.get("fault_type") or metadata.get("fault_type"),
        location=location,
        evidence=list(findings.get("evidence") or metadata.get("evidence") or []),
        confidence=float(diagnosis.confidence or 0.0),
        reasoning=diagnosis.reasoning,
        tool_calls=list(metadata.get("tool_calls") or findings.get("tool_calls") or []),
        time_taken_seconds=float(metadata.get("time_taken_seconds", 0.0) or 0.0),
        metadata=metadata,
    )


def _build_ground_truth(scenario: ScenarioHandle, index: int) -> dict[str, Any]:
    episodes = scenario.episodes
    episode = episodes[min(index, len(episodes) - 1)] if episodes else {}
    fault_type = episode.get("fault_type") or scenario.metadata.get("expected_diagnosis")
    location = {}
    if episode.get("target_device"):
        location["device"] = episode.get("target_device")
    if episode.get("target_interface"):
        location["interface"] = episode.get("target_interface")
    ground_truth: dict[str, Any] = {}
    if fault_type:
        ground_truth["fault_type"] = fault_type
    if location:
        ground_truth["location"] = location
    return ground_truth


def _agent_name(diagnosis_results: list[DiagnosisResult]) -> str:
    return diagnosis_results[0].agent_name if diagnosis_results else "unknown"


def _suite_scale(scenarios: Iterable[ScenarioHandle]) -> str:
    scales = {scenario.scale for scenario in scenarios}
    return next(iter(scales)) if len(scales) == 1 else "mixed"


__all__ = ["EvaluatorManager", "create_fault_type_judge_evaluator_adapter"]
