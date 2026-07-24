"""Parse and persist canonical single-episode scenario files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from netopsbench.models.scenario import ScenarioSpec
from netopsbench.platform.faults.specs import canonicalize_fault_name
from netopsbench.platform.utils.files import atomic_write_text


def scenario_from_dict(data: dict[str, Any]) -> ScenarioSpec:
    if "episodes" in data:
        raise ValueError(
            "Legacy multi-episode scenarios are unsupported; regenerate the scenario with a single 'episode'"
        )
    payload = dict(data)
    episode = dict(payload.get("episode") or {})
    episode["fault_type"] = canonicalize_fault_name(episode.get("fault_type", "none"))
    payload["episode"] = episode
    try:
        return ScenarioSpec.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Invalid scenario schema: {exc}") from exc


def parse_scenario_file(file_path: str | Path) -> ScenarioSpec:
    path = Path(file_path)
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid scenario YAML {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Scenario payload must be a mapping: {path}")
    return scenario_from_dict(payload)


def save_scenario_file(scenario: ScenarioSpec, file_path: str | Path) -> Path:
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = scenario.model_dump(mode="json", exclude_none=True)
    atomic_write_text(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    return path


__all__ = ["parse_scenario_file", "save_scenario_file", "scenario_from_dict"]
