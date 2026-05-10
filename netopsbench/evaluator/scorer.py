"""
Evaluator - Scoring system for agent troubleshooting results.

Compares agent outputs against ground truth and generates benchmark reports.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from netopsbench.evaluator.fault_type_judge import FaultTypeJudge, canonicalize_fault_type, judge_fault_type_match
from netopsbench.platform.utils.interface_names import normalize_interface_name

# Composite localization score weighting (used for sorting/ranking only).
# device contributes 60%, interface contributes 40%; both scores are still
# reported separately as the primary tier-1 KPIs.
DEVICE_LOCALIZATION_WEIGHT: float = 0.6
INTERFACE_LOCALIZATION_WEIGHT: float = 0.4


@dataclass
class AgentOutput:
    """Output from an AI agent's troubleshooting attempt."""

    verdict: str  # "fault_detected" | "network_healthy" | "inconclusive"
    fault_type: str | None = None
    location: dict[str, str] | None = None  # {"device": str, "interface": str | None}
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    time_taken_seconds: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    # metadata convention for token reporting:
    #   metadata["input_tokens"]  -> int: prompt tokens consumed by the LLM
    #   metadata["output_tokens"] -> int: completion tokens produced by the LLM

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvaluationResult:
    """Result of evaluating an agent's output against ground truth."""

    testcase_id: str
    correct_verdict: bool
    correct_device: bool
    correct_interface: bool
    correct_fault_type: bool
    score: float
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def create_default_evaluator() -> Evaluator:
    """Stable tiny factory boundary for public evaluator adapters."""
    return Evaluator()


def create_fault_type_judge_evaluator(fault_type_judge: FaultTypeJudge) -> Evaluator:
    """Create an evaluator that uses a semantic judge for fault-type matching."""
    return Evaluator(fault_type_judge=fault_type_judge)


class Evaluator:
    """
    Evaluates agent troubleshooting results against ground truth.

    Scoring dimensions:
    - Verdict accuracy (fault detected vs network healthy)
    - Device localization accuracy
    - Interface localization accuracy (if applicable)
    - Fault type identification accuracy
    - Tool call efficiency
    """

    def __init__(self, fault_type_judge: FaultTypeJudge | None = None):
        # Localization-first KPI:
        # - Primary: device localization rate
        # - Secondary: interface localization rate (for interface-applicable cases)
        # - Composite (sorting only): see DEVICE_/INTERFACE_LOCALIZATION_WEIGHT.
        self.weights = {
            "device": DEVICE_LOCALIZATION_WEIGHT,
            "interface": INTERFACE_LOCALIZATION_WEIGHT,
        }
        self.fault_type_judge = fault_type_judge

    def evaluate(self, agent_output: AgentOutput, ground_truth: dict[str, Any], testcase_id: str) -> EvaluationResult:
        """
        Evaluate agent output against ground truth.

        Args:
            agent_output: The agent's troubleshooting output
            ground_truth: Expected correct answer from test case
            testcase_id: ID of the test case

        Returns:
            EvaluationResult with scores and details
        """
        # Check verdict
        expected_verdict = "fault_detected" if ground_truth else "network_healthy"
        correct_verdict = agent_output.verdict == expected_verdict

        # If no fault (negative sample), only verdict matters
        if not ground_truth:
            score = 1.0 if correct_verdict else 0.0
            return EvaluationResult(
                testcase_id=testcase_id,
                correct_verdict=correct_verdict,
                correct_device=True,  # N/A for healthy network
                correct_interface=True,
                correct_fault_type=True,
                score=score,
                details={
                    "type": "negative_sample",
                    "negative_sample": True,
                    "agent_verdict": agent_output.verdict,
                    "expected_verdict": expected_verdict,
                    "false_positive": not correct_verdict,
                },
            )

        # For fault cases, evaluate localization accuracy
        gt_location = ground_truth.get("location", {})
        equivalent_locations = ground_truth.get("equivalent_locations") or []
        gt_interface = gt_location.get("interface")
        gt_fault_type = ground_truth.get("fault_type", "")

        agent_location = agent_output.location or {}
        agent_device = agent_location.get("device", "")
        agent_interface = agent_location.get("interface")
        agent_fault_type = agent_output.fault_type or ""

        candidate_locations = [gt_location]
        candidate_locations.extend(location for location in equivalent_locations if isinstance(location, dict))
        correct_device, correct_interface, matched_location, location_match_mode = self._match_location(
            agent_device=agent_device,
            agent_interface=agent_interface,
            candidate_locations=candidate_locations,
            interface_applicable=bool(gt_interface),
        )

        # Check fault type accuracy. By default this is deterministic canonical matching;
        # when configured, an LLM judge is used only for non-exact semantic cases.
        correct_fault_type, fault_type_judgment = judge_fault_type_match(
            judge=self.fault_type_judge,
            agent_fault_type=agent_fault_type,
            ground_truth_fault_type=gt_fault_type,
            agent_reasoning=agent_output.reasoning,
            evidence=agent_output.evidence,
        )

        interface_applicable = bool(gt_interface)
        # Fault-case score is verdict-gated: no localization score unless fault is detected.
        if not correct_verdict:
            score = 0.0
        elif interface_applicable:
            score = (self.weights["device"] if correct_device else 0.0) + (
                self.weights["interface"] if correct_interface else 0.0
            )
        else:
            # If interface GT is unavailable, use device localization as full score.
            score = 1.0 if correct_device else 0.0

        return EvaluationResult(
            testcase_id=testcase_id,
            correct_verdict=correct_verdict,
            correct_device=correct_device,
            correct_interface=correct_interface,
            correct_fault_type=correct_fault_type,
            score=round(score, 3),
            details={
                "agent_output": agent_output.to_dict(),
                "ground_truth": ground_truth,
                "tool_calls_count": len(agent_output.tool_calls),
                "time_taken": agent_output.time_taken_seconds,
                "confidence": agent_output.confidence,
                "inconclusive": agent_output.verdict == "inconclusive",
                "interface_applicable": interface_applicable,
                "equivalent_locations": equivalent_locations,
                "matched_location": matched_location,
                "location_match_mode": location_match_mode,
                "fault_type_judgment": fault_type_judgment,
            },
        )

    def _match_location(
        self,
        agent_device: str | None,
        agent_interface: str | None,
        candidate_locations: list[dict[str, Any]],
        interface_applicable: bool,
    ) -> tuple[bool, bool, dict[str, Any] | None, str]:
        """Choose the best-matching acceptable location for scoring."""
        best_rank: tuple[int, int, int] = (-1, -1, -1)
        best_device = False
        best_interface = not interface_applicable
        best_location: dict[str, Any] | None = None
        best_mode = "no_match"

        for index, location in enumerate(candidate_locations):
            candidate_device = location.get("device", "")
            candidate_interface = location.get("interface")
            device_match = self._normalize_name(agent_device or "") == self._normalize_name(candidate_device or "")
            if interface_applicable:
                interface_match = self._normalize_interface(agent_interface) == self._normalize_interface(
                    candidate_interface
                )
            else:
                interface_match = True

            rank: tuple[int, int, int] = (
                1 if device_match else 0,
                1 if interface_match else 0,
                1 if index == 0 else 0,
            )
            if not self._is_better_location_rank(rank, best_rank):
                continue

            best_rank = rank
            best_device = device_match
            best_interface = interface_match
            best_location = location
            if device_match and interface_match:
                best_mode = "exact" if index == 0 else "equivalent"
            else:
                best_mode = "partial"

        return best_device, best_interface, best_location, best_mode

    def _is_better_location_rank(self, rank: tuple[int, int, int], best_rank: tuple[int, int, int]) -> bool:
        """Return True when a location rank should replace the current best rank."""
        for value, best_value in zip(rank, best_rank, strict=True):
            if value > best_value:
                return True
            if value < best_value:
                return False
        return False

    def _normalize_name(self, name: str) -> str:
        """Normalize device/interface names for comparison."""
        if not name:
            return ""
        return name.lower().strip().replace("-", "").replace("_", "")

    def _normalize_interface(self, interface: str | None) -> str:
        """Normalize interface names (e.g., 'ethernet-1/1' -> 'e11')."""
        return normalize_interface_name(interface)

    def _normalize_fault_type(self, fault_type: str) -> str:
        """Normalize fault type names for comparison."""
        return canonicalize_fault_type(fault_type)

    def _is_fully_correct_case(self, result: EvaluationResult) -> bool:
        """Return True when a testcase is fully solved end-to-end."""
        if result.details.get("negative_sample"):
            return result.correct_verdict

        interface_applicable = bool((result.details.get("ground_truth", {}).get("location", {}) or {}).get("interface"))
        return (
            result.correct_verdict
            and result.correct_device
            and result.correct_fault_type
            and (result.correct_interface if interface_applicable else True)
        )

    def generate_report(
        self, results: list[EvaluationResult], agent_name: str = "unknown", topology_scale: str = "unknown"
    ) -> dict[str, Any]:
        """
        Generate a benchmark report from evaluation results.

        Args:
            results: List of evaluation results
            agent_name: Name of the agent being evaluated
            topology_scale: Scale of the network topology

        Returns:
            Benchmark report dictionary
        """
        if not results:
            return {"error": "No results to report"}

        total = len(results)
        negative_cases = [r for r in results if r.details.get("negative_sample")]
        positive_cases = [r for r in results if not r.details.get("negative_sample")]
        positive_total = len(positive_cases)
        correct_verdict = sum(1 for r in results if r.correct_verdict)
        # Primary KPI (Option A): denominator is all positive cases; numerator requires both verdict and device correctness.
        correct_device = sum(1 for r in positive_cases if r.correct_verdict and r.correct_device)
        correct_fault_type = sum(1 for r in positive_cases if r.correct_fault_type)
        localization_success_rate = round(correct_device / positive_total, 3) if positive_total else 0.0
        interface_applicable_cases = sum(
            1
            for r in positive_cases
            if bool((r.details.get("ground_truth", {}).get("location", {}) or {}).get("interface"))
        )
        correct_interface_applicable = sum(
            1
            for r in positive_cases
            if bool((r.details.get("ground_truth", {}).get("location", {}) or {}).get("interface"))
            and r.correct_verdict
            and r.correct_interface
        )
        correct_interface = correct_interface_applicable
        interface_localization_rate = round(
            (correct_interface_applicable / interface_applicable_cases) if interface_applicable_cases else 0.0,
            3,
        )
        localization_composite_score = round(
            (DEVICE_LOCALIZATION_WEIGHT * localization_success_rate)
            + (INTERFACE_LOCALIZATION_WEIGHT * interface_localization_rate),
            3,
        )
        fault_type_accuracy = round(correct_fault_type / positive_total, 3) if positive_total else 0.0
        detection_accuracy = round(correct_verdict / total, 3)
        overall_accuracy = round(
            sum(1 for r in results if self._is_fully_correct_case(r)) / total,
            3,
        )

        # Calculate by difficulty
        by_difficulty = {}
        for result in results:
            difficulty = result.details.get("difficulty", "unknown")
            if difficulty not in by_difficulty:
                by_difficulty[difficulty] = {"total": 0, "score_sum": 0.0}
            by_difficulty[difficulty]["total"] += 1
            by_difficulty[difficulty]["score_sum"] += result.score

        for diff in by_difficulty:
            by_difficulty[diff]["avg_score"] = round(by_difficulty[diff]["score_sum"] / by_difficulty[diff]["total"], 3)

        # Calculate by fault type
        by_fault_type = {}
        for result in results:
            gt = result.details.get("ground_truth", {})
            fault_type = gt.get("fault_type", "unknown") if gt else "negative"
            if fault_type not in by_fault_type:
                by_fault_type[fault_type] = {"total": 0, "correct": 0, "score_sum": 0.0}
            by_fault_type[fault_type]["total"] += 1
            by_fault_type[fault_type]["score_sum"] += result.score
            if gt and result.correct_fault_type:
                by_fault_type[fault_type]["correct"] += 1

        for ft in by_fault_type:
            by_fault_type[ft]["accuracy"] = round(by_fault_type[ft]["correct"] / by_fault_type[ft]["total"], 3)
            by_fault_type[ft]["avg_score"] = round(by_fault_type[ft]["score_sum"] / by_fault_type[ft]["total"], 3)

        # Tool usage stats
        total_tool_calls = sum(len(r.details.get("agent_output", {}).get("tool_calls", [])) for r in results)
        avg_tool_calls = round(total_tool_calls / total, 1) if total > 0 else 0

        # Time stats
        total_time = sum(
            r.details.get("agent_output", {}).get("time_taken_seconds", 0) or r.details.get("time_taken", 0)
            for r in results
        )
        avg_time = round(total_time / total, 1) if total > 0 else 0

        # Token stats (sourced from AgentOutput.metadata["input_tokens"/"output_tokens"])
        total_input_tokens = sum(
            int(r.details.get("agent_output", {}).get("metadata", {}).get("input_tokens", 0) or 0) for r in results
        )
        total_output_tokens = sum(
            int(r.details.get("agent_output", {}).get("metadata", {}).get("output_tokens", 0) or 0) for r in results
        )
        avg_input_tokens = round(total_input_tokens / total, 1) if total > 0 else 0
        avg_output_tokens = round(total_output_tokens / total, 1) if total > 0 else 0

        # Negative sample (false positive / false negative) stats
        false_positives = sum(1 for r in negative_cases if r.details.get("false_positive"))
        false_negatives = sum(
            1 for r in positive_cases if r.details.get("agent_output", {}).get("verdict") == "network_healthy"
        )
        false_positive_rate = round(false_positives / len(negative_cases), 3) if negative_cases else None
        false_negative_rate = round(false_negatives / len(positive_cases), 3) if positive_cases else None

        report = {
            "benchmark_id": f"netopsbench-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
            "timestamp": datetime.now().isoformat(),
            "agent_name": agent_name,
            "topology_scale": topology_scale,
            "summary": {
                "total_cases": total,
                "correct_verdict": correct_verdict,
                "correct_device": correct_device,
                "correct_interface": correct_interface,
                "correct_fault_type": correct_fault_type,
                # End-to-end testcase accuracy across healthy and faulty cases.
                "overall_accuracy": overall_accuracy,
                "detection_accuracy": detection_accuracy,
                # Tier-1 primary KPI.
                "localization_success_rate": localization_success_rate,
                "device_localization_rate": localization_success_rate,
                "interface_localization_rate": interface_localization_rate,
                "interface_applicable_cases": interface_applicable_cases,
                "localization_composite_score": localization_composite_score,
                # Alias field for existing dashboards.
                "device_accuracy": localization_success_rate,
                # Tier-2 secondary KPI.
                "fault_type_accuracy": fault_type_accuracy,
                "average_score": round(sum(r.score for r in results) / total, 3),
                "avg_tool_calls": avg_tool_calls,
                "avg_time_seconds": avg_time,
                "total_input_tokens": total_input_tokens,
                "avg_input_tokens_per_case": avg_input_tokens,
                "total_output_tokens": total_output_tokens,
                "avg_output_tokens_per_case": avg_output_tokens,
                "false_positive_rate": false_positive_rate,
                "false_negative_rate": false_negative_rate,
                "negative_sample_cases": len(negative_cases),
                "positive_sample_cases": positive_total,
            },
            "breakdown_by_difficulty": by_difficulty,
            "breakdown_by_fault_type": by_fault_type,
            "detailed_results": [r.to_dict() for r in results],
        }

        return report

    def save_report(self, report: dict[str, Any], filepath: str) -> None:
        """Save benchmark report to JSON file."""
        with open(filepath, "w") as f:
            json.dump(report, f, indent=2, default=str)

    def load_report(self, filepath: str) -> dict[str, Any]:
        """Load benchmark report from JSON file."""
        with open(filepath) as f:
            return json.load(f)
