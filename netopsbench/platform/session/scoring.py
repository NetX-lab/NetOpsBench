"""Scenario scoring helpers."""

from pathlib import Path
from typing import Any

from netopsbench.evaluator.scorer import AgentOutput, EvaluationResult, Evaluator
from netopsbench.models.topology import DeviceRole
from netopsbench.platform.topology.topology_utils import load_topology_manifest
from netopsbench.platform.utils.interface_names import are_interfaces_equivalent, to_sonic_interface

# Fault types where the injected interface and its link-peer are both valid answers.
# link_down / link_flapping: the link can be attributed to either endpoint.
# packet_loss / packet_corruption / high_latency: interface-level impairments are
#   observable from both sides of the link, so the peer endpoint is equivalent.
# mtu_mismatch: misconfiguration requires both ends to match; either endpoint is a
#   valid root-cause answer.
_INTERFACE_SYMMETRIC_FAULT_TYPES = {
    "link_down",
    "link_flapping",
    "packet_loss",
    "packet_corruption",
    "high_latency",
    "mtu_mismatch",
}


def _find_link_peer_locations(
    topology_dir: str | None,
    target_device: str | None,
    target_interface: str | None,
) -> list[dict[str, str]]:
    if not topology_dir or not target_device or not target_interface:
        return []
    manifest = load_topology_manifest(Path(topology_dir))

    def location(device: str, interface: str) -> dict[str, str]:
        peer = manifest.device(device)
        if peer is not None and peer.role is not DeviceRole.CLIENT:
            interface = to_sonic_interface(interface)
        return {"device": device, "interface": interface}

    for link in manifest.links:
        left, right = link.endpoints
        if left.device == target_device and are_interfaces_equivalent(left.interface, target_interface):
            return [location(right.device, right.interface)]
        if right.device == target_device and are_interfaces_equivalent(right.interface, target_interface):
            return [location(left.device, left.interface)]
    return []


def build_episode_ground_truth(episode_info: dict[str, Any], topology_dir: str | None = None) -> dict[str, Any]:
    location = {"device": episode_info.get("target_device")}
    if episode_info.get("target_interface"):
        location["interface"] = episode_info.get("target_interface")
    ground_truth = {"fault_type": episode_info.get("fault_type"), "location": location}
    if episode_info.get("fault_type") in _INTERFACE_SYMMETRIC_FAULT_TYPES:
        equivalent_locations = _find_link_peer_locations(
            topology_dir=topology_dir,
            target_device=episode_info.get("target_device"),
            target_interface=episode_info.get("target_interface"),
        )
        if equivalent_locations:
            ground_truth["equivalent_locations"] = equivalent_locations
    return ground_truth


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
