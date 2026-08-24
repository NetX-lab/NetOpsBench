"""Canonical diagnostic context helpers shared by every incident entrypoint."""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from netopsbench.platform.toolkit.toolkit import AgentToolkit

_EPISODE_ALLOWED_KEYS = {
    "duration_seconds",
    "stabilization_time",
}
_MAX_CANONICAL_ANOMALIES = 12
_MODEL_FORBIDDEN_KEYS = frozenset(
    {
        "case_id",
        "scenario_id",
        "scenario",
        "scenario_filename",
        "expected",
        "expected_result",
        "ground_truth",
        "fault_type",
        "target_device",
        "target_interface",
        "target_prefix",
        "injection",
        "evaluation_result",
        "testcase_id",
        "episode_id",
        "match",
        "correct_verdict",
        "correct_device",
        "correct_interface",
        "correct_fault_type",
    }
)

# Scenario execution must retain injection metadata internally for rollback and
# evaluator scoring, but operator-visible tool output must not announce that a
# fault was injected.  These markers were present in v2 device-log payloads and
# made the exploratory comparison non-blind.  Redact only the marker phrase;
# preserve the surrounding operational observation.
_MODEL_FORBIDDEN_VALUE_PATTERNS = (
    (re.compile(r"(?i)\bnetopsbench[-_ ]+injected\b"), "configured"),
    (re.compile(r"(?i)\binjected\s+at\b"), "observed at"),
    (re.compile(r"(?i)\bfault\s+injection\b"), "fault operation"),
    (re.compile(r"(?i)\bcontainer\s+[^\s]+\s+is\s+not\s+running\b"), "endpoint unavailable"),
    (re.compile(r"(?i)\bcontainer\s+[^\s]+\s+not\s+found\b"), "endpoint unavailable"),
    (re.compile(r"(?i)\bdocker\s+exec\b[^\n]*"), "endpoint operation unavailable"),
)


def build_topology_snapshot(toolkit: AgentToolkit) -> dict:
    topology_result = toolkit.get_topology()
    if hasattr(topology_result, "success") and topology_result.success:
        return topology_result.data
    return {"devices": {}}


def extract_episode_pingmesh_query_window(episode_result: dict[str, Any]) -> dict[str, str | None]:
    observations = episode_result.get("observations", {}) if isinstance(episode_result, dict) else {}
    if not isinstance(observations, dict):
        return {"start_time": None, "end_time": None}
    start_time = observations.get("start_time")
    end_time = observations.get("end_time")
    if isinstance(start_time, str) and isinstance(end_time, str) and start_time and end_time:
        return {"start_time": start_time, "end_time": end_time}
    pingmesh_metrics = observations.get("pingmesh_metrics", {}) if isinstance(observations, dict) else {}
    windows = pingmesh_metrics.get("windows", {}) if isinstance(pingmesh_metrics, dict) else {}
    if isinstance(windows, dict) and windows:
        current_starts = []
        current_ends = []
        for window in windows.values():
            if not isinstance(window, dict):
                continue
            current = window.get("current", {})
            if not isinstance(current, dict):
                continue
            start_candidate = current.get("start")
            end_candidate = current.get("end")
            if isinstance(start_candidate, str) and start_candidate:
                current_starts.append(start_candidate)
            if isinstance(end_candidate, str) and end_candidate:
                current_ends.append(end_candidate)
        if current_starts and current_ends:
            return {"start_time": min(current_starts), "end_time": max(current_ends)}
    return {"start_time": None, "end_time": None}


def _bounded_observations(observations: dict[str, Any]) -> dict[str, Any]:
    """Return the complete public observation payload.

    Model-specific prompt compaction belongs to the canonical observation
    consumer.  It must not silently alter ``DiagnosticContext.symptoms`` for
    third-party benchmark agents.
    """
    return copy.deepcopy(observations)


def _compact_model_observations(observations: dict[str, Any]) -> dict[str, Any]:
    """Compact only the optional model-facing canonical view."""
    compacted = copy.deepcopy(observations)
    metrics = compacted.get("pingmesh_metrics")
    if not isinstance(metrics, dict):
        return compacted
    metrics.pop("aggregated_anomalies", None)
    anomalies = metrics.get("anomalies")
    if not isinstance(anomalies, list):
        return compacted
    severity_rank = {"high": 2, "medium": 1, "low": 0}
    persistence_rank = {"persistent": 3, "steady_only": 2, "early_only": 1, "full_window": 0}

    def rank(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            -severity_rank.get(str(item.get("severity")), 0),
            -persistence_rank.get(str(item.get("persistence")), 0),
            -float(item.get("value", 0.0) or 0.0),
            str(item.get("type", "")),
            str(item.get("src_ip", "")),
            str(item.get("dst_ip", "")),
        )

    selected = sorted(
        (item for item in anomalies if isinstance(item, dict)),
        key=rank,
    )[:_MAX_CANONICAL_ANOMALIES]
    metrics["anomalies"] = selected
    metrics["returned_anomalies"] = len(selected)
    metrics["truncated"] = len(selected) < len(anomalies)
    return compacted


def build_public_case_id(*, scenario_id: str, episode_result: dict[str, Any]) -> str:
    """Return a stable, non-semantic case id for agent context."""
    episode = episode_result.get("episode", {}) if isinstance(episode_result, dict) else {}
    episode_id = episode.get("episode_id") if isinstance(episode, dict) else None
    source = f"{scenario_id}:{episode_id or 'unknown'}"
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
    return f"case-{digest}"


def build_public_symptoms(*, episode_result: dict[str, Any], pingmesh_query_window: dict[str, Any]) -> dict[str, Any]:
    """Build the bounded symptom payload exposed to a diagnosis agent."""
    episode = episode_result.get("episode", {}) if isinstance(episode_result, dict) else {}
    observations = episode_result.get("observations", {}) if isinstance(episode_result, dict) else {}
    safe_episode = (
        {key: episode.get(key) for key in _EPISODE_ALLOWED_KEYS if key in episode} if isinstance(episode, dict) else {}
    )
    return {
        "episode": safe_episode,
        "observations": _bounded_observations(observations) if isinstance(observations, dict) else {},
        "pingmesh_query_window": pingmesh_query_window if isinstance(pingmesh_query_window, dict) else {},
        "observation_type": "scenario_episode",
    }


def build_canonical_observation(
    *,
    case_id: str | None = None,
    topology: dict[str, Any],
    symptoms: dict[str, Any],
    include_case_id: bool = False,
) -> dict[str, Any]:
    """Build the shared model-visible observation for benchmark and simulator agents."""
    devices = topology.get("devices", {}) if isinstance(topology, dict) else {}
    if not isinstance(devices, dict):
        devices = {}

    def count(group: str) -> int:
        entries = devices.get(group, [])
        return len(entries) if isinstance(entries, list) else 0

    family = topology.get("topology_type") or topology.get("family") or "unknown"
    is_fat_tree = family == "fat-tree"
    links = topology.get("links", []) if isinstance(topology, dict) else []
    source_symptoms = symptoms if isinstance(symptoms, dict) else {}
    canonical_symptoms = {
        "episode": copy.deepcopy(source_symptoms.get("episode", {})),
        "observations": _compact_model_observations(source_symptoms.get("observations", {})),
        "pingmesh_query_window": copy.deepcopy(source_symptoms.get("pingmesh_query_window", {})),
    }
    observation = {
        "topology_summary": {
            "family": str(family),
            "spines": 0 if is_fat_tree else count("spines"),
            "leafs": 0 if is_fat_tree else count("leafs"),
            "cores": count("cores") if is_fat_tree else 0,
            "aggs": count("aggs") if is_fat_tree else 0,
            "edges": count("edges") if is_fat_tree else 0,
            "clients": count("clients"),
            "links": len(links) if isinstance(links, list) else 0,
        },
        "symptoms": canonical_symptoms,
    }
    if include_case_id and case_id:
        observation["case_id"] = str(case_id)
    if not include_case_id:
        assert_model_visible_payload(observation)
    return observation


def assert_model_visible_payload(payload: Any) -> None:
    """Reject evaluator/scenario-control fields from model-visible payloads."""

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).strip().lower() in _MODEL_FORBIDDEN_KEYS:
                    raise ValueError(f"model-visible payload contains forbidden field at {path}.{key}")
                walk(child, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(payload, "$")


def sanitize_model_visible_payload(payload: Any) -> Any:
    """Return a deep copy with evaluator/scenario-control fields removed."""

    if isinstance(payload, dict):
        return {
            str(key): sanitize_model_visible_payload(value)
            for key, value in payload.items()
            if str(key).strip().lower() not in _MODEL_FORBIDDEN_KEYS
        }
    if isinstance(payload, list):
        return [sanitize_model_visible_payload(value) for value in payload]
    if isinstance(payload, tuple):
        return tuple(sanitize_model_visible_payload(value) for value in payload)
    if isinstance(payload, str):
        value = payload
        for pattern, replacement in _MODEL_FORBIDDEN_VALUE_PATTERNS:
            value = pattern.sub(replacement, value)
        return value
    return payload


__all__ = [
    "build_canonical_observation",
    "build_public_case_id",
    "build_public_symptoms",
    "build_topology_snapshot",
    "assert_model_visible_payload",
    "sanitize_model_visible_payload",
]
