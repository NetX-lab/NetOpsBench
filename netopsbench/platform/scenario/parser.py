"""
Scenario File Parser - Parses YAML scenario files
"""

from pathlib import Path
from typing import Any

import yaml

from netopsbench.platform.faults.specs import canonicalize_fault_name

from .models import Episode, Scenario


def episode_to_dict(episode: Episode) -> dict[str, Any]:
    return {
        "episode_id": episode.episode_id,
        "description": getattr(episode, "description", ""),
        "fault_type": canonicalize_fault_name(getattr(episode, "fault_type", "none")),
        "target_device": getattr(episode, "target_device", None),
        "target_interface": getattr(episode, "target_interface", None),
        "target_prefix": getattr(episode, "target_prefix", None),
        "mtu": getattr(episode, "mtu", None),
        "duration_seconds": getattr(episode, "duration_seconds", 30),
        "stabilization_time": getattr(episode, "stabilization_time", 10),
        "metadata": dict(getattr(episode, "metadata", {}) or {}),
        "parameters": dict(getattr(episode, "parameters", {}) or {}),
    }


def episode_from_dict(data: dict[str, Any]) -> Episode:
    return Episode(
        episode_id=data["episode_id"],
        description=data.get("description", ""),
        fault_type=canonicalize_fault_name(data.get("fault_type", "none")),
        target_device=data.get("target_device"),
        target_interface=data.get("target_interface"),
        target_prefix=data.get("target_prefix"),
        mtu=data.get("mtu"),
        duration_seconds=data.get("duration_seconds", 30),
        stabilization_time=data.get("stabilization_time", 10),
        metadata=dict(data.get("metadata", {}) or {}),
        parameters=dict(data.get("parameters", {}) or {}),
    )


def scenario_to_dict(scenario: Scenario) -> dict[str, Any]:
    return {
        "scenario_id": scenario.scenario_id,
        "name": scenario.name,
        "description": scenario.description,
        "topology_scale": scenario.topology_scale,
        "traffic_profile": scenario.traffic_profile,
        "metadata": dict(scenario.metadata or {}),
        "parameters": dict(scenario.parameters or {}),
        "episodes": [episode_to_dict(episode) for episode in scenario.episodes],
    }


def scenario_from_dict(data: dict[str, Any]) -> Scenario:
    episodes = [episode_from_dict(ep_data) for ep_data in data.get("episodes", [])]
    return Scenario(
        scenario_id=data["scenario_id"],
        name=data["name"],
        description=data.get("description", ""),
        topology_scale=data.get("topology_scale", "xs"),
        traffic_profile=data.get("traffic_profile", "standard"),
        episodes=episodes,
        metadata=dict(data.get("metadata", {}) or {}),
        parameters=dict(data.get("parameters", {}) or {}),
    )


def parse_scenario_file(file_path: str | Path) -> Scenario:
    with open(file_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return scenario_from_dict(data)


def save_scenario_file(scenario: Scenario, file_path: str | Path) -> Path:
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = scenario_to_dict(scenario)
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True)
    return path
